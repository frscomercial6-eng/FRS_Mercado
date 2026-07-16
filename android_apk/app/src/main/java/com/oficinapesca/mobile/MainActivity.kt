package com.frs.mercado

import android.content.Intent
import android.content.Context
import android.os.Bundle
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import com.google.android.gms.auth.GoogleAuthUtil
import com.google.android.gms.auth.api.signin.GoogleSignIn
import com.google.android.gms.auth.api.signin.GoogleSignInAccount
import com.google.android.gms.auth.api.signin.GoogleSignInOptions
import com.google.android.gms.common.api.Scope
import java.text.SimpleDateFormat
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.ArrayDeque
import java.util.Base64
import java.util.Date
import java.util.Locale
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import org.json.JSONArray
import org.json.JSONObject

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private val tokenFileNames = listOf(BuildConfig.TOKEN_FILE_NAME.trim().ifBlank { "acesso.token" })
    private val driveScope = "https://www.googleapis.com/auth/drive.readonly"
    private val driveTokenFolderId = BuildConfig.DRIVE_TOKEN_FOLDER_ID.trim()
    private val tokenSecret = BuildConfig.TOKEN_SECRET.trim()
    private val authPrefs by lazy { getSharedPreferences("oficina_pesca_google_auth", Context.MODE_PRIVATE) }
    private var lastDriveDebugMessage: String = ""
    private val signInLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val task = GoogleSignIn.getSignedInAccountFromIntent(result.data)
        try {
            val account = task.result
            if (account != null) {
                validarLicencaNoDrive(account)
            } else {
                exibirBloqueioSemDrive("Não foi possível autenticar no Google Drive.")
            }
        } catch (exc: Exception) {
            Log.w("OficinaPesca", "Falha no login Google: ${exc.message}")
            exibirBloqueioSemDrive("Falha ao autenticar no Google Drive. Tente novamente.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Dark Mode padrão no container.
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_YES)

        webView = WebView(this)
        setContentView(webView)

        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.loadsImagesAutomatically = true
        settings.allowFileAccess = true
        settings.allowContentAccess = true
        settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW

        webView.webChromeClient = WebChromeClient()
        webView.addJavascriptInterface(FirebaseBridge(), "OficinaFirebase")

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                return false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                val json = FirebaseBridge().firebaseJson().replace("\\", "\\\\").replace("'", "\\'")
                view?.evaluateJavascript(
                    "window.__OFP_FIREBASE_CONFIG__ = JSON.parse('$json');window.dispatchEvent(new Event('ofp-firebase-config-ready'));",
                    null
                )
                super.onPageFinished(view, url)
            }
        }

        iniciarFluxoMobileIndependente()
    }

    private fun iniciarFluxoMobileIndependente() {
        webView.loadUrl(
            "data:text/html," + URLEncoder.encode(
                """
                <!DOCTYPE html>
                <html lang='pt-BR'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'>
                <title>Oficina de Pesca</title>
                <style>
                    body{margin:0;background:#0f1923;color:#ecf0f1;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
                    .box{max-width:520px;background:#1a2535;border-radius:16px;padding:26px;text-align:center}
                    h2{margin-top:0;color:#93c5fd}
                </style></head>
                <body><div class='box'><h2>Conectando ao Google Drive...</h2><p>Validando licença mobile de forma independente do Desktop.</p><p>v${BuildConfig.VERSION_NAME}</p></div></body></html>
                """.trimIndent(),
                Charsets.UTF_8.name()
            )
        )

        val account = GoogleSignIn.getLastSignedInAccount(this)
        if (account != null && possuiEscopoDrive(account)) {
            validarLicencaNoDrive(account)
            return
        }

        tentarLoginSilencioso()
    }

    private fun tentarLoginSilencioso() {
        val gso = GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
            .requestEmail()
            .requestScopes(Scope(driveScope))
            .build()
        val client = GoogleSignIn.getClient(this, gso)

        client.silentSignIn()
            .addOnSuccessListener { account ->
                if (account != null) {
                    onLoginGoogleConcluido(account)
                } else {
                    solicitarLoginGoogle()
                }
            }
            .addOnFailureListener {
                solicitarLoginGoogle()
            }
    }

    private fun solicitarLoginGoogle() {
        val gso = GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
            .requestEmail()
            .requestScopes(Scope(driveScope))
            .build()
        val client = GoogleSignIn.getClient(this, gso)
        signInLauncher.launch(client.signInIntent)
    }

    private fun possuiEscopoDrive(account: GoogleSignInAccount): Boolean {
        return GoogleSignIn.hasPermissions(account, Scope(driveScope))
    }

    private fun validarLicencaNoDrive(account: GoogleSignInAccount) {
        Thread {
            val resultado = try {
                val conta = account.account
                if (conta == null) {
                    lastDriveDebugMessage = "Conta Google indisponível para gerar token."
                    false
                } else {
                    val emailLogado = (account.email ?: "").trim().lowercase(Locale.ROOT)
                    if (emailLogado.isBlank()) {
                        lastDriveDebugMessage = "E-mail da conta Google não disponível para validar o token."
                        false
                    } else {
                        val token = GoogleAuthUtil.getToken(this, conta, "oauth2:$driveScope")
                        val arquivo = localizarArquivoToken(token)
                        if (arquivo == null) {
                            lastDriveDebugMessage = if (lastDriveDebugMessage.isNotBlank()) lastDriveDebugMessage else "Arquivo de token não encontrado no Drive."
                            false
                        } else {
                            val conteudo = baixarConteudoArquivoDrive(arquivo.fileId, token)
                            if (conteudo == null) {
                                false
                            } else {
                                validarConteudoToken(conteudo, emailLogado).also { valido ->
                                    if (valido) {
                                        lastDriveDebugMessage = ""
                                    }
                                }
                            }
                        }
                    }
                }
            } catch (exc: Exception) {
                Log.w("OficinaPesca", "Falha ao validar licença no Drive: ${exc.message}")
                lastDriveDebugMessage = interpretarErroDrive(exc)
                false
            }

            runOnUiThread {
                if (resultado) {
                    persistirSessaoGoogle(account)
                    // Independência total do Desktop: a licença é lida do Drive e validada localmente.
                    webView.loadUrl(BuildConfig.WEB_APP_URL)
                } else {
                    val debug = if (lastDriveDebugMessage.isNotBlank()) lastDriveDebugMessage else "Falha ao autenticar no Google Drive."
                    exibirBloqueioSemDrive(
                        if (debug.contains("não encontrado", ignoreCase = true)) "Arquivo de token não encontrado no Drive." else "Licença expirada ou inválida",
                        debug
                    )
                }
            }
        }.start()
    }

    private fun onLoginGoogleConcluido(account: GoogleSignInAccount) {
        if (possuiEscopoDrive(account)) {
            validarLicencaNoDrive(account)
        } else {
            solicitarLoginGoogle()
        }
    }

    private fun persistirSessaoGoogle(account: GoogleSignInAccount) {
        val email = (account.email ?: "").trim()
        if (email.isBlank()) {
            return
        }
        authPrefs.edit()
            .putString("last_google_email", email)
            .putBoolean("last_google_drive_ok", true)
            .apply()
    }

    private data class DriveTokenFile(val fileId: String, val fileName: String)

    private fun localizarArquivoToken(token: String): DriveTokenFile? {
        val folderId = driveTokenFolderId
        if (folderId.isNotBlank()) {
            val file = localizarArquivoEmSubarvore(folderId, token)
            if (file != null) {
                lastDriveDebugMessage = ""
                return file
            }
            lastDriveDebugMessage = "Arquivo de token não encontrado na pasta configurada nem em subpastas."
            return null
        }

        val nameQuery = tokenFileNames.joinToString(" or ") { "name='${it}'" }
        val globalQuery = "trashed=false and ($nameQuery)"
        val globalFiles = executarConsultaDrive(globalQuery, token)
        if (globalFiles != null && globalFiles.length() > 0) {
            for (i in 0 until globalFiles.length()) {
                val file = globalFiles.optJSONObject(i) ?: continue
                val id = file.optString("id", "").trim()
                val name = file.optString("name", "").trim()
                if (id.isNotBlank()) {
                    lastDriveDebugMessage = ""
                    return DriveTokenFile(id, name)
                }
            }
        }

        lastDriveDebugMessage = "Arquivo de token não encontrado no Drive."
        return null
    }

    private fun localizarArquivoEmSubarvore(folderId: String, token: String): DriveTokenFile? {
        val fila = ArrayDeque<String>()
        val visitados = mutableSetOf<String>()
        fila.add(folderId)

        while (fila.isNotEmpty()) {
            val pastaAtual = fila.removeFirst()
            if (!visitados.add(pastaAtual)) {
                continue
            }

            val nameQuery = tokenFileNames.joinToString(" or ") { "name='${it}'" }
            val fileQuery = "trashed=false and '$pastaAtual' in parents and ($nameQuery)"
            val files = executarConsultaDrive(fileQuery, token)
            if (files != null && files.length() > 0) {
                for (i in 0 until files.length()) {
                    val file = files.optJSONObject(i) ?: continue
                    val id = file.optString("id", "").trim()
                    val name = file.optString("name", "").trim()
                    if (id.isNotBlank()) {
                        return DriveTokenFile(id, name)
                    }
                }
            }

            val folderQuery = "trashed=false and mimeType='application/vnd.google-apps.folder' and '$pastaAtual' in parents"
            val folders = executarConsultaDrive(folderQuery, token)
            if (folders == null || folders.length() == 0) {
                continue
            }

            for (i in 0 until folders.length()) {
                val folder = folders.optJSONObject(i) ?: continue
                val id = folder.optString("id", "").trim()
                if (id.isNotBlank() && !visitados.contains(id)) {
                    fila.add(id)
                }
            }
        }

        return null
    }

    private fun baixarConteudoArquivoDrive(fileId: String, token: String): String? {
        return try {
            val encodedId = URLEncoder.encode(fileId, Charsets.UTF_8.name())
            val url = URL("https://www.googleapis.com/drive/v3/files/$encodedId?alt=media&supportsAllDrives=true")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.connectTimeout = 7000
            conn.readTimeout = 7000
            conn.setRequestProperty("Authorization", "Bearer $token")
            conn.setRequestProperty("Accept", "text/plain,application/json")
            conn.setRequestProperty("User-Agent", "OficinaPescaWebView/${BuildConfig.VERSION_NAME}")

            val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (conn.responseCode !in 200..299) {
                lastDriveDebugMessage = interpretarHttpDrive(conn.responseCode, body)
                Log.w("OficinaPesca", "Drive download erro HTTP ${conn.responseCode}: $body")
                return null
            }

            body
        } catch (exc: Exception) {
            lastDriveDebugMessage = interpretarErroDrive(exc)
            Log.w("OficinaPesca", "Falha ao baixar licença do Drive: ${exc.message}")
            null
        }
    }

    private data class TokenValidacao(val valida: Boolean, val mensagem: String)

    private fun validarConteudoToken(conteudoBruto: String, emailLogado: String): Boolean {
        val conteudo = normalizarConteudoToken(conteudoBruto)
        val validacao = validarTokenAcesso(conteudo, emailLogado)
        if (!validacao.valida) {
            lastDriveDebugMessage = validacao.mensagem
            return false
        }
        return true
    }

    private fun normalizarConteudoToken(conteudo: String): String {
        val texto = conteudo.trim()
        if (texto.startsWith("{")) {
            return try {
                val json = JSONObject(texto)
                listOf(
                    json.optString("token", ""),
                    json.optString("access_token", ""),
                    json.optString("chave", "")
                ).firstOrNull { it.isNotBlank() }?.trim() ?: texto
            } catch (_: Exception) {
                texto
            }
        }
        return texto.replace("\r", "").trim()
    }

    private fun validarTokenAcesso(tokenBruto: String, emailLogado: String): TokenValidacao {
        val token = tokenBruto.replace("\n", "").replace("\r", "").trim()
        if (token.isBlank()) {
            return TokenValidacao(false, "Arquivo de token vazio no Drive.")
        }
        if (tokenSecret.isBlank()) {
            return TokenValidacao(false, "Segredo de token não configurado no APK.")
        }
        if (!token.startsWith("OFP-TKN-")) {
            return TokenValidacao(false, "Formato de token inválido no arquivo do Drive.")
        }

        val partes = token.split("-", limit = 4)
        if (partes.size != 4) {
            return TokenValidacao(false, "Arquivo de token incompleto no Drive.")
        }

        val payloadB64 = partes[2]
        val assinaturaRecebida = partes[3].trim().uppercase(Locale.ROOT)
        val assinaturaEsperada = assinarPayloadToken(payloadB64)
        if (!assinaturaEsperada.equals(assinaturaRecebida, ignoreCase = true)) {
            return TokenValidacao(false, "Assinatura do token inválida.")
        }

        val payloadJson = try {
            val padding = "=".repeat((4 - payloadB64.length % 4) % 4)
            val bytes = Base64.getUrlDecoder().decode(payloadB64 + padding)
            String(bytes, Charsets.UTF_8)
        } catch (exc: Exception) {
            return TokenValidacao(false, "Conteúdo do token inválido: ${exc.message}")
        }

        val payload = try {
            JSONObject(payloadJson)
        } catch (exc: Exception) {
            return TokenValidacao(false, "Payload JSON do token inválido: ${exc.message}")
        }

        val uid = payload.optString("uid", "").trim()
        if (uid.isBlank()) {
            return TokenValidacao(false, "Campo uid ausente no token.")
        }
        if (!uid.equals(emailLogado.trim().lowercase(Locale.ROOT), ignoreCase = true)) {
            return TokenValidacao(false, "Token não pertence ao e-mail autenticado: $emailLogado")
        }

        val validade = payload.optString("exp", "").trim()
        if (!Regex("^\\d{4}-\\d{2}-\\d{2}$").matches(validade)) {
            return TokenValidacao(false, "Data de expiração inválida no token.")
        }
        val hoje = SimpleDateFormat("yyyy-MM-dd", Locale.ROOT).format(Date())
        if (validade < hoje) {
            return TokenValidacao(false, "Token expirado em ${validade}.")
        }

        return TokenValidacao(true, "Token válido.")
    }

    private fun assinarPayloadToken(payloadB64: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        val keySpec = SecretKeySpec(tokenSecret.toByteArray(Charsets.UTF_8), "HmacSHA256")
        mac.init(keySpec)
        val assinatura = mac.doFinal(payloadB64.toByteArray(Charsets.UTF_8))
        return assinatura.joinToString("") { byte -> "%02X".format(byte) }.take(20)
    }

    private fun executarConsultaDrive(query: String, token: String): JSONArray? {
        return try {
            val encodedQ = URLEncoder.encode(query, Charsets.UTF_8.name())
            val url = URL("https://www.googleapis.com/drive/v3/files?q=$encodedQ&fields=files(id,name,mimeType,parents)&pageSize=1000&supportsAllDrives=true&includeItemsFromAllDrives=true")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.connectTimeout = 7000
            conn.readTimeout = 7000
            conn.setRequestProperty("Authorization", "Bearer $token")
            conn.setRequestProperty("Accept", "application/json")
            conn.setRequestProperty("User-Agent", "OficinaPescaWebView/${BuildConfig.VERSION_NAME}")

            val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
            val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (conn.responseCode !in 200..299) {
                lastDriveDebugMessage = interpretarHttpDrive(conn.responseCode, body)
                Log.w("OficinaPesca", "Drive API erro HTTP ${conn.responseCode}: $body")
                return null
            }

            lastDriveDebugMessage = ""
            val json = JSONObject(body)
            json.optJSONArray("files")
        } catch (exc: Exception) {
            lastDriveDebugMessage = interpretarErroDrive(exc)
            Log.w("OficinaPesca", "Falha na consulta ao Drive API: ${exc.message}")
            null
        }
    }

    private fun interpretarHttpDrive(codigo: Int, corpo: String): String {
        val resumo = when (codigo) {
            400 -> "400 Bad Request"
            401 -> "401 Unauthorized"
            403 -> "403 Forbidden"
            404 -> "404 Not Found"
            429 -> "429 Too Many Requests"
            in 500..599 -> "$codigo Server Error"
            else -> "$codigo HTTP Error"
        }
        return if (corpo.isNotBlank()) {
            "$resumo - ${corpo.take(180)}"
        } else {
            resumo
        }
    }

    private fun interpretarErroDrive(exc: Exception): String {
        return when {
            exc is SecurityException -> "403 Forbidden - ${exc.message ?: "acesso negado"}"
            exc.message?.contains("403", ignoreCase = true) == true -> "403 Forbidden - ${exc.message}"
            exc.message?.contains("404", ignoreCase = true) == true -> "404 Not Found - ${exc.message}"
            exc.message?.contains("401", ignoreCase = true) == true -> "401 Unauthorized - ${exc.message}"
            else -> exc.message ?: "Falha desconhecida no Google Drive"
        }
    }

    private fun exibirBloqueioSemDrive(mensagem: String, debug: String = "") {
        webView.loadUrl(
            "data:text/html," + URLEncoder.encode(
                """
                <!DOCTYPE html>
                <html lang='pt-BR'>
                <head>
                  <meta charset='UTF-8'>
                  <meta name='viewport' content='width=device-width, initial-scale=1.0'>
                  <title>Acesso Bloqueado</title>
                  <style>
                    body { background:#0f1923; color:#ecf0f1; display:flex; align-items:center; justify-content:center; min-height:100vh; font-family:Arial,sans-serif; margin:0; }
                    .box { max-width:540px; background:#1a2535; border-radius:20px; padding:32px 24px; text-align:center; box-shadow:0 8px 32px rgba(0,0,0,.45); }
                    h1 { color:#f39c12; margin-top:0; }
                    p { color:#d0d6dc; }
                  </style>
                </head>
                <body>
                  <div class='box'>
                    <h1>Acesso Bloqueado</h1>
                    <p>${mensagem.replace("<", "&lt;").replace(">", "&gt;")}</p>
                    <p>Este APK valida a licença direto no Google Drive, sem depender do Desktop.</p>
                                        <p style='margin-top:14px;font-size:12px;color:#9fb0bf;'>${debug.replace("<", "&lt;").replace(">", "&gt;")}</p>
                    <p>v${BuildConfig.VERSION_NAME}</p>
                  </div>
                </body>
                </html>
                """.trimIndent(),
                Charsets.UTF_8.name()
            )
        )
    }

    private fun baseUrl(): String {
        val url = BuildConfig.WEB_APP_URL.trim()
        val parsed = URL(url)
        val port = if (parsed.port > 0) ":${parsed.port}" else ""
        return "${parsed.protocol}://${parsed.host}$port"
    }

    private fun localBlockedHtml(): String {
        return "data:text/html," + java.net.URLEncoder.encode(
            """
            <!DOCTYPE html>
            <html lang='pt-BR'>
            <head>
              <meta charset='UTF-8'>
              <meta name='viewport' content='width=device-width, initial-scale=1.0'>
              <title>Licença Bloqueada</title>
              <style>
                body { background:#0f1923; color:#ecf0f1; display:flex; align-items:center; justify-content:center; min-height:100vh; font-family:Arial,sans-serif; margin:0; }
                .box { max-width:520px; background:#1a2535; border-radius:20px; padding:32px 24px; text-align:center; box-shadow:0 8px 32px rgba(0,0,0,.45); }
                h1 { color:#f39c12; margin-top:0; }
              </style>
            </head>
            <body>
              <div class='box'>
                <h1>Acesso Bloqueado</h1>
                <p>Não foi possível validar a licença deste sistema no servidor.</p>
                <p>Verifique a licença no Desktop ou a conectividade com a oficina.</p>
                <p>v${BuildConfig.VERSION_NAME}</p>
              </div>
            </body>
            </html>
            """.trimIndent(),
            Charsets.UTF_8.name()
        )
    }

        private fun trialPlansHtml(baseUrl: String): String {
                val html = """
                        <!DOCTYPE html>
                        <html lang='pt-BR'>
                        <head>
                            <meta charset='UTF-8'>
                            <meta name='viewport' content='width=device-width, initial-scale=1.0'>
                            <title>Planos Oficina de Pesca</title>
                            <style>
                                body { background:#181a1b; color:#f5f5f5; font-family:Segoe UI, Arial, sans-serif; margin:0; padding:24px; }
                                .wrap { max-width:1080px; margin:0 auto; }
                                h1 { color:#f5f5f5; margin:0 0 10px 0; font-size:32px; }
                                .sub { color:#ff9f43; margin-bottom:20px; font-weight:600; }
                                .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
                                .card { background:#202325; border:1px solid #3b3b3b; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(0,0,0,.24); }
                                .promo { border:2px solid #00E676; }
                                .best { border:3px solid #FF9F43; }
                                .name { font-size:18px; font-weight:700; margin-bottom:10px; }
                                .price { font-size:24px; font-weight:800; margin-bottom:8px; }
                                .desc { color:#d5dbe1; min-height:54px; }
                                .cta { display:inline-block; margin-top:14px; text-decoration:none; background:#2196f3; color:#fff; padding:12px 16px; border-radius:12px; font-weight:700; }
                                .cta.best { background:#FF9F43; color:#000; }
                                .footer { margin-top:22px; color:#9fb0bf; font-size:14px; }
                            </style>
                        </head>
                        <body>
                            <div class='wrap'>
                                <h1>Escolha o melhor plano para sua oficina</h1>
                                <div class='sub'>Seu acesso atual está em Trial. A navegação completa será liberada após ativação.</div>
                                <div class='grid'>
                                    ${if (BuildConfig.PROMO_ATIVA) "<div class='card promo'><div class='name'>PROMOCIONAL</div><div class='price'>R$ ${BuildConfig.PROMO_VALOR}</div><div class='desc'>${BuildConfig.PROMO_NOME}</div><a class='cta' href='${BuildConfig.PLANO_LINK_PROMO}'>Assinar agora</a></div>" else ""}
                                    <div class='card'><div class='name'>MENSAL</div><div class='price'>R$ 69,90</div><div class='desc'>Acesso imediato ao sistema.</div><a class='cta' href='${BuildConfig.PLANO_LINK_MENSAL}'>Assinar agora</a></div>
                                    <div class='card'><div class='name'>TRIMESTRAL</div><div class='price'>R$ 179,90</div><div class='desc'>Ideal para começar.</div><a class='cta' href='${BuildConfig.PLANO_LINK_TRIMESTRAL}'>Assinar agora</a></div>
                                    <div class='card best'><div class='name'>SEMESTRAL</div><div class='price'>R$ 359,90</div><div class='desc'>Melhor escolha para economia.</div><a class='cta best' href='${BuildConfig.PLANO_LINK_SEMESTRAL}'>Assinar agora</a></div>
                                    <div class='card'><div class='name'>ANUAL</div><div class='price'>R$ 799,90</div><div class='desc'>Plano profissional de 12 meses.</div><a class='cta' href='${BuildConfig.PLANO_LINK_ANUAL}'>Assinar agora</a></div>
                                </div>
                                <div class='footer'>Aguardando confirmação automática do pagamento. Assim que a licença for ativada, o sistema completo será liberado.</div>
                            </div>
                            <script>
                                async function verificarLiberacao() {
                                    try {
                                        const resp = await fetch('${baseUrl}/api/licenca-status', { cache: 'no-store' });
                                        const data = await resp.json();
                                        const trial = Boolean(data && data.trial_ativo);
                                        const ativa = Boolean(data && (data.licenca_ativa || data.ativa));
                                        if (ativa && !trial) {
                                            window.location.replace('${BuildConfig.WEB_APP_URL}');
                                        }
                                    } catch (_) {}
                                }
                                setInterval(verificarLiberacao, 8000);
                            </script>
                        </body>
                        </html>
                """.trimIndent()

                return "data:text/html," + java.net.URLEncoder.encode(html, Charsets.UTF_8.name())
        }

    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    inner class FirebaseBridge {
        @JavascriptInterface
        fun firebaseJson(): String {
            val payload = JSONObject()
            payload.put("apiKey", BuildConfig.FIREBASE_API_KEY)
            payload.put("authDomain", BuildConfig.FIREBASE_AUTH_DOMAIN)
            payload.put("databaseURL", BuildConfig.FIREBASE_DATABASE_URL)
            payload.put("projectId", BuildConfig.FIREBASE_PROJECT_ID)
            payload.put("storageBucket", BuildConfig.FIREBASE_STORAGE_BUCKET)
            payload.put("messagingSenderId", BuildConfig.FIREBASE_MESSAGING_SENDER_ID)
            payload.put("appId", BuildConfig.FIREBASE_APP_ID)
            payload.put("syncChannel", BuildConfig.FIREBASE_SYNC_CHANNEL)
            return payload.toString()
        }
    }
}
