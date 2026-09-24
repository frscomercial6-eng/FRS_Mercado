import os
import subprocess
import threading
import time
from pathlib import Path

from modulo_config import carregar_configuracoes
from license_manager import LicenseManager


def _es_nome_instalador_acbr(nome: str) -> bool:
    """True se o nome corresponde a um instalador (-I/DEMO/installer),
    nunca ao binario real do motor fiscal."""
    nome_low = str(nome or "").lower()
    return (
        "installer" in nome_low
        or "demo" in nome_low
        or nome_low.endswith("-i.exe")
    )


class SystemMonitor:
    def __init__(self, on_status=None, interval_seconds=6):
        self.on_status = on_status
        self.interval_seconds = max(3, int(interval_seconds))
        self._stop_event = threading.Event()
        self._thread = None
        self._ultimo_alerta_inicio = 0.0
        self._license_manager = LicenseManager()
        # Instalação do ACBr Monitor DEMO oficial (motor fiscal ausente).
        self._instalacao_acbr_em_andamento = False
        self._ultima_tentativa_instalacao_acbr = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.is_set():
            status = self.collect_status()
            if callable(self.on_status):
                try:
                    self.on_status(status)
                except Exception:
                    pass
            time.sleep(self.interval_seconds)

    def collect_status(self):
        lic = self._license_manager.get_status()
        cfg = carregar_configuracoes()
        fiscal_ativo = bool(cfg.get("fiscal_ativo", False))

        acbr_running = self._is_acbr_running()
        fiscal_ok = self._is_fiscal_connection_ok(cfg)
        iniciou_servico = False

        alerta = ""
        cor = "#ff5555"
        status_txt = "Fiscal: Desativado"

        if fiscal_ativo:
            if acbr_running and fiscal_ok:
                status_txt = "Fiscal: Ativo"
                cor = "#3b82f6"
            else:
                status_txt = "Fiscal: Offline"
                cor = "#ff6666"
                alerta = "Integrador Fiscal offline - Iniciando serviço..."
                iniciou_servico = self._try_start_acbr(cfg)
                if iniciou_servico:
                    status_txt = "Fiscal: Inicializando"
                    cor = "#f1c40f"

        fiscal_text = status_txt
        fiscal_color = cor

        lic_text = str(lic.get("message") or "Licença: Indisponível")
        lic_color = str(lic.get("color") or "#f1c40f")
        lic_expirada = bool(lic.get("is_expired", False))
        lic_alerta = bool(lic.get("is_warning", False))

        header_text = f"{lic_text} | {fiscal_text}"
        header_color = lic_color if (lic_expirada or lic_alerta) else fiscal_color

        return {
            "fiscal_ativo": fiscal_ativo,
            "acbr_running": acbr_running,
            "fiscal_ok": fiscal_ok,
            "status_text": fiscal_text,
            "status_color": fiscal_color,
            "license_text": lic_text,
            "license_color": lic_color,
            "license_expired": lic_expirada,
            "license_warning": lic_alerta,
            "license_days_left": lic.get("days_left"),
            "renewal_url": lic.get("renewal_url"),
            "header_text": header_text,
            "header_color": header_color,
            "alerta": alerta,
            "iniciou_servico": iniciou_servico,
        }

    def _is_acbr_running(self):
        try:
            resultado = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            saida = (resultado.stdout or "").lower()
            return "acbrmonitor" in saida
        except Exception:
            return False

    def _is_fiscal_connection_ok(self, cfg):
        try:
            pasta_in = Path(str(cfg.get("pasta_entrada_fiscal") or "")).resolve()
            pasta_out = Path(str(cfg.get("pasta_retorno_fiscal") or "")).resolve()
            if not pasta_in.exists() or not pasta_out.exists():
                return False

            teste = pasta_in / "_monitor_healthcheck.tmp"
            teste.write_text("ok", encoding="utf-8")
            teste.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def _try_start_acbr(self, cfg):
        agora = time.time()
        # Evita tentativa agressiva a cada ciclo.
        if (agora - self._ultimo_alerta_inicio) < 30:
            return False
        self._ultimo_alerta_inicio = agora

        candidatos = []
        emissor_cfg = str(cfg.get("emissor_fiscal_path") or "").strip()
        if emissor_cfg:
            candidatos.append(Path(emissor_cfg))

        # Pastas candidatas com a mesma resolução do runtime fiscal
        # (_pasta_base_aplicacao: base do executável quando congelado).
        try:
            from modulo_fiscal import _pastas_instala_candidatas

            pastas_instala = _pastas_instala_candidatas()
        except Exception:
            pastas_instala = [Path(__file__).resolve().parent / "instala"]
        for pasta_instala in pastas_instala:
            candidatos.extend(
                [
                    pasta_instala / "ACBrMonitorPLUS.exe",
                    pasta_instala / "ACBrMonitor.exe",
                ]
            )
            # Excluye instaladores (-I/DEMO): nunca deben lanzarse como motor fiscal.
            try:
                candidatos.extend(
                    arq
                    for arq in pasta_instala.glob("*ACBrMonitor*.exe")
                    if not _es_nome_instalador_acbr(arq.name)
                )
            except Exception:
                continue

        for exe in candidatos:
            try:
                if exe and exe.exists() and exe.suffix.lower() == ".exe":
                    subprocess.Popen(
                        [str(exe)],
                        cwd=str(exe.parent),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    return True
            except Exception:
                continue

        # Detecção ampliada: destinos reais do instalador oficial DEMO
        # (InstallLocation no registro + diretórios padrão do ACBrMonitorPLUS).
        # Instaladores nunca são considerados motor.
        motor_ampliado = self._localizar_motor_ampliado()
        if motor_ampliado is not None:
            try:
                subprocess.Popen(
                    [str(motor_ampliado)],
                    cwd=str(motor_ampliado.parent),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return True
            except Exception:
                pass

        # Motor fiscal realmente ausente: agenda a instalação do ACBr Monitor
        # DEMO oficial empacotado (mesmos parâmetros do setup Inno).
        if self._agendar_instalacao_acbr():
            return True

        return False

    def _localizar_motor_ampliado(self):
        """Localiza o motor além dos candidatos tradicionais (registro/DEMO padrão)."""
        try:
            from modulo_fiscal import localizar_acbr_instalado

            return localizar_acbr_instalado()
        except Exception:
            return None

    def _agendar_instalacao_acbr(self):
        """Agenda a instalação do ACBr DEMO oficial quando o motor está ausente."""
        if self._instalacao_acbr_em_andamento:
            return False

        # ACBr já instalado? NÃO executar o instalador.
        try:
            from modulo_fiscal import localizar_acbr_instalado

            if localizar_acbr_instalado():
                return False
        except Exception:
            return False

        agora = time.time()
        # Nova tentativa de instalação no máximo a cada 5 minutos.
        if (agora - self._ultima_tentativa_instalacao_acbr) < 300:
            return False
        self._ultima_tentativa_instalacao_acbr = agora
        self._instalacao_acbr_em_andamento = True
        threading.Thread(target=self._instalar_acbr_e_iniciar, daemon=True).start()
        return True

    def _instalar_acbr_e_iniciar(self):
        """Instala o DEMO oficial, aplica a configuração existente e inicia o motor."""
        try:
            from modulo_fiscal import instalar_acbr_demo, localizar_acbr_instalado

            executavel = localizar_acbr_instalado()
            if executavel is None:
                executavel = instalar_acbr_demo()
            if executavel is None:
                return

            # Aplica a configuração existente do FRS (pastas fiscal_in/fiscal_out
            # e ACBrMonitor.ini nos locais corretos, inclusive o do ACBr).
            try:
                from modulo_fiscal import FiscalManager

                FiscalManager()
            except Exception:
                pass

            subprocess.Popen(
                [str(executavel)],
                cwd=str(executavel.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        finally:
            self._instalacao_acbr_em_andamento = False
