import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webhook_security import registrar_rejeicao_integracao, validar_token_webhook


_lock = threading.Lock()
_server = None
_server_thread = None
_callbacks = []


class _DeliveryWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Evita poluir stdout do app desktop com logs HTTP.
        return

    def _send_json(self, status_code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        ip = getattr(self, "client_address", ["desconhecido"])[0]

        if self.path != "/receber_pedido_externo":
            registrar_rejeicao_integracao("Rota inválida", ip=ip, path=self.path)
            self._send_json(404, {"ok": False, "erro": "Rota não encontrada"})
            return

        token_header = self.headers.get("X-Webhook-Token", "")
        ok_token, msg_token = validar_token_webhook(token_header)
        if not ok_token:
            registrar_rejeicao_integracao(msg_token, ip=ip, path=self.path)
            self._send_json(401, {"ok": False, "erro": "Não autorizado"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0

        if content_length <= 0:
            registrar_rejeicao_integracao("Body vazio", ip=ip, path=self.path)
            self._send_json(400, {"ok": False, "erro": "Body vazio"})
            return

        if content_length > 2 * 1024 * 1024:
            registrar_rejeicao_integracao("Payload muito grande", ip=ip, path=self.path)
            self._send_json(413, {"ok": False, "erro": "Payload muito grande"})
            return

        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            registrar_rejeicao_integracao("JSON inválido", ip=ip, path=self.path)
            self._send_json(400, {"ok": False, "erro": "JSON inválido"})
            return

        if not isinstance(payload, dict):
            registrar_rejeicao_integracao("JSON fora do formato objeto", ip=ip, path=self.path)
            self._send_json(400, {"ok": False, "erro": "JSON deve ser um objeto"})
            return

        callbacks = list(_callbacks)
        for cb in callbacks:
            try:
                cb(payload)
            except Exception:
                # Falhas de callback não devem derrubar o webhook.
                pass

        self._send_json(200, {"ok": True, "mensagem": "Pedido recebido"})


def iniciar_servidor_webhook(callback, host="127.0.0.1", port=8765):
    """Inicia servidor interno para pedidos externos e registra callback de entrega."""
    global _server, _server_thread

    with _lock:
        if callback not in _callbacks:
            _callbacks.append(callback)

        if _server is not None:
            return {"host": host, "port": port, "ativo": True}

        _server = ThreadingHTTPServer((host, port), _DeliveryWebhookHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()

        return {"host": host, "port": port, "ativo": True}


def parar_servidor_webhook():
    global _server, _server_thread

    with _lock:
        if _server is not None:
            try:
                _server.shutdown()
            except Exception:
                pass
            try:
                _server.server_close()
            except Exception:
                pass
        _server = None
        _server_thread = None
        _callbacks.clear()
