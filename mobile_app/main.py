import flet as ft
import os
from pathlib import Path
from urllib.parse import unquote

from firebase_config import buscar_produto_por_codigo


def _configurar_caminho_chave_firebase() -> None:
    """Define FIREBASE_ADMIN_KEY_PATH quando a chave estiver no app local/empacotado."""
    if str(os.getenv("FIREBASE_ADMIN_KEY_PATH", "") or "").strip():
        return

    base = Path(__file__).resolve().parent
    cwd = Path.cwd()

    candidatos = [
        base / "firebase-admin-key.json",
        base / "assets" / "firebase-admin-key.json",
        cwd / "firebase-admin-key.json",
        cwd / "assets" / "firebase-admin-key.json",
    ]

    for caminho in candidatos:
        if caminho.exists() and caminho.is_file():
            os.environ["FIREBASE_ADMIN_KEY_PATH"] = str(caminho.resolve())
            return


def main(page: ft.Page):
    _configurar_caminho_chave_firebase()

    page.title = "FRS Mercado Mobile"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0F1115"
    page.padding = 16
    page.window_width = 420
    page.window_height = 760

    titulo = ft.Text("FRS Mercado Mobile", size=24, weight=ft.FontWeight.BOLD)
    subtitulo = ft.Text("Consulta rapida por codigo de barras", color=ft.Colors.GREY_400)

    campo_codigo = ft.TextField(
        label="Codigo de barras",
        hint_text="Digite ou bipa o codigo...",
        autofocus=True,
        border_radius=12,
    )

    foto_placeholder = ft.Container(
        width=220,
        height=220,
        border_radius=16,
        bgcolor="#1B1F27",
        alignment=ft.Alignment(0, 0),
        content=ft.Column(
            controls=[
                ft.Icon(ft.Icons.IMAGE_OUTLINED, size=48, color=ft.Colors.GREY_500),
                ft.Text("Foto do produto", color=ft.Colors.GREY_500),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        ),
    )

    nome_produto = ft.Text("", size=20, weight=ft.FontWeight.W_600)
    preco_produto = ft.Text("", size=18, color="#7BFFB3")
    status = ft.Text("", color=ft.Colors.GREY_400)

    def _aplicar_codigo_lido(codigo: str) -> None:
        codigo_limpo = str(codigo or "").strip()
        if not codigo_limpo:
            return
        campo_codigo.value = codigo_limpo
        page.update()
        consultar(None)

    def _tratar_retorno_scan(route: str) -> None:
        rota = str(route or "").strip()
        if not rota:
            return

        # Suporta frsmercado://scan/<codigo> e variações com query ?code=
        if "code=" in rota:
            codigo = rota.split("code=", 1)[1].split("&", 1)[0]
            _aplicar_codigo_lido(unquote(codigo))
            return

        marker = "/scan/"
        if marker in rota:
            codigo = rota.split(marker, 1)[1].split("?", 1)[0]
            _aplicar_codigo_lido(unquote(codigo))

    def escanear(_e):
        status.value = "Abrindo camera para leitura..."
        page.update()

        # Fluxo Android via app Barcode Scanner (ZXing) com retorno por deep link.
        # Se o app scanner nao estiver instalado, o Android exibira app nao encontrado.
        page.launch_url("zxing://scan/?ret=frsmercado://scan/%s", web_popup_window_name="_self")

    def _on_route_change(e: ft.RouteChangeEvent):
        _tratar_retorno_scan(e.route)

    page.on_route_change = _on_route_change
    _tratar_retorno_scan(page.route)

    def consultar(_e):
        codigo = campo_codigo.value or ""
        if not codigo.strip():
            status.value = "Informe um codigo para consultar."
            nome_produto.value = ""
            preco_produto.value = ""
            page.update()
            return

        status.value = "Consultando Firestore..."
        nome_produto.value = ""
        preco_produto.value = ""
        page.update()

        try:
            produto = buscar_produto_por_codigo(codigo)
            if not produto:
                status.value = "Produto nao encontrado na colecao produtos."
                page.update()
                return

            nome_produto.value = produto["nome"]
            preco_produto.value = f"R$ {produto['preco']:.2f}"
            status.value = "Consulta concluida."

            if produto.get("foto_url"):
                foto_placeholder.content = ft.Image(
                    src=produto["foto_url"],
                    fit=ft.ImageFit.COVER,
                    border_radius=16,
                )
            else:
                foto_placeholder.content = ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED_OUTLINED, size=48, color=ft.Colors.GREY_500),
                        ft.Text("Sem foto cadastrada", color=ft.Colors.GREY_500),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                )

        except Exception as e:
            status.value = f"Erro ao consultar: {e}"

        page.update()

    btn_consultar = ft.ElevatedButton(
        "Consultar",
        on_click=consultar,
        icon=ft.Icons.SEARCH,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
    )

    btn_escanear = ft.OutlinedButton(
        "Escanear",
        on_click=escanear,
        icon=ft.Icons.QR_CODE_SCANNER,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
    )

    page.add(
        ft.Column(
            controls=[
                titulo,
                subtitulo,
                ft.Divider(color="#222833"),
                campo_codigo,
                ft.Row(
                    controls=[btn_consultar, btn_escanear],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Container(height=12),
                ft.Row([foto_placeholder], alignment=ft.MainAxisAlignment.CENTER),
                ft.Container(height=10),
                nome_produto,
                preco_produto,
                status,
            ],
            spacing=10,
            expand=True,
        )
    )


if __name__ == "__main__":
    ft.app(target=main)
