import os
import subprocess
import threading
import time
from pathlib import Path

from modulo_config import carregar_configuracoes
from license_manager import LicenseManager


class SystemMonitor:
    def __init__(self, on_status=None, interval_seconds=6):
        self.on_status = on_status
        self.interval_seconds = max(3, int(interval_seconds))
        self._stop_event = threading.Event()
        self._thread = None
        self._ultimo_alerta_inicio = 0.0
        self._license_manager = LicenseManager()

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

        pasta_instala = Path(__file__).resolve().parent / "instala"
        candidatos.extend(
            [
                pasta_instala / "ACBrMonitorPLUS.exe",
                pasta_instala / "ACBrMonitor.exe",
            ]
        )
        candidatos.extend(list(pasta_instala.glob("*ACBrMonitor*.exe")))

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

        return False
