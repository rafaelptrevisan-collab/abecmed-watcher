#!/usr/bin/env python3
"""
abecmed-watch — avisa quando o catalogo do bot da ABECMED muda.

O site (bot.abecmed.com.br) e um Typebot. Este script percorre o fluxo
de conversa como se fosse um navegador, junta o texto das telas de
produto num relatorio, e compara com a execucao anterior. Se mudou,
notifica via ntfy.sh.

Nao faz parsing de produto/preco de proposito: compara o texto inteiro.
Menos coisa pra quebrar quando a ABECMED mexer no fluxo.

Configuracao, em ordem de prioridade:
    1. variaveis de ambiente ABECMED_CPF e NTFY_TOPIC (usado no GitHub Actions)
    2. arquivo config.ini ao lado deste script (use ./configurar.sh)

Uso:
    ./run.sh --loop                       # ou run.bat no Windows
    python3 bot.py --dry-run          # roda, imprime, nao notifica nem salva
"""

import argparse
import configparser
import difflib
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = "https://bot.abecmed.com.br"
TYPEBOT = "pix-pagamento"
NTFY_BASE = "https://ntfy.sh"

# Cabecalhos identicos aos que o Firefox manda nesse site. Alem de evitar
# bloqueio por protecao anti-bot da Vercel, mantem a requisicao consistente:
# UA de navegador com headers de script destoa mais do que nao mascarar nada.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TIMEOUT = 30
RETRIES = 3

# Intervalo do modo --loop, em minutos. Sorteado a cada ciclo.
INTERVALO_MIN = 7
INTERVALO_MAX = 12

# Blocos do richText que viram quebra de linha no texto plano.
BLOCK_TYPES = {"p", "ul", "ol", "li", "h1", "h2", "h3", "blockquote"}

# Limite de mensagem do ntfy (4096 bytes). Deixo folga.
MAX_MSG = 3500


class FlowError(Exception):
    """O fluxo do Typebot nao esta onde a gente esperava."""


# ---------------------------------------------------------------- HTTP


def post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    base_headers = dict(BROWSER_HEADERS)
    base_headers.update(headers or {})
    # Valor None = remover o header (usado pra tirar o disfarce ao falar com o ntfy).
    base_headers = {k: v for k, v in base_headers.items() if v is not None}

    last_error = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=body, method="POST", headers=base_headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 4xx nao adianta repetir.
            if exc.code < 500:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise FlowError("HTTP %s em %s: %s" % (exc.code, url, detail)) from exc
        except Exception as exc:  # rede, timeout, JSON invalido
            last_error = exc
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)

    raise FlowError("falhou apos %d tentativas em %s: %s" % (RETRIES, url, last_error))


# ------------------------------------------------------- richText -> texto


def flatten(node):
    """Achata a arvore richText do Typebot em texto plano legivel."""
    if isinstance(node, list):
        return "".join(flatten(n) for n in node)
    if not isinstance(node, dict):
        return ""

    children = node.get("children")
    if children is None:
        return node.get("text", "")

    inner = flatten(children)
    kind = node.get("type")

    if kind == "li":
        return "- " + inner.strip() + "\n"
    if kind in BLOCK_TYPES:
        return inner + "\n"
    return inner


def messages_to_text(messages):
    """Junta as bolhas de uma resposta num bloco de texto."""
    parts = []
    for msg in messages:
        content = msg.get("content") or {}
        if content.get("type") == "richText":
            parts.append(flatten(content.get("richText", [])))
        elif "url" in content:
            parts.append("[%s] %s" % (msg.get("type", "midia"), content["url"]))
    return tidy("\n".join(parts))


def tidy(text):
    """Tira espaco no fim das linhas e colapsa linhas em branco repetidas."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


# ------------------------------------------------------------- conversa


class Chat:
    def __init__(self):
        self.last = post_json(
            "%s/api/v1/typebots/%s/startChat" % (BASE, TYPEBOT),
            {"isStreamEnabled": False, "prefilledVariables": {}, "isOnlyRegistering": False},
            headers={"Origin": BASE, "Referer": BASE + "/"},
        )
        self.session_id = self.last.get("sessionId")
        if not self.session_id:
            raise FlowError("startChat nao devolveu sessionId")

    @property
    def items(self):
        payload = self.last.get("input") or {}
        return [i.get("content", "") for i in payload.get("items", [])]

    @property
    def input_type(self):
        return (self.last.get("input") or {}).get("type")

    @property
    def text(self):
        return messages_to_text(self.last.get("messages", []))

    def send(self, text):
        self.last = post_json(
            "%s/api/v1/sessions/%s/continueChat" % (BASE, self.session_id),
            {"message": {"type": "text", "text": text}},
            headers={"Origin": BASE, "Referer": BASE + "/"},
        )
        return self.last

    def pick(self, pattern, required=True):
        """Escolhe a opcao cujo texto casa com o regex. Devolve o texto escolhido."""
        rx = re.compile(pattern, re.I)
        for content in self.items:
            if rx.search(content):
                self.send(content)
                return content
        if required:
            raise FlowError(
                "nenhuma opcao casou com /%s/ — opcoes: %r" % (pattern, self.items)
            )
        return None


# ----------------------------------------------------------------- fluxo

MENU_HINT = re.compile(r"flor|concentrad|[óo]leo", re.I)
GO_ON = re.compile(r"ciente|entendi|continuar|prosseguir|ok", re.I)


def reach_menu(chat, max_hops=5):
    """Avanca por telas intermediarias ate chegar no menu principal."""
    for _ in range(max_hops):
        if any(MENU_HINT.search(i) for i in chat.items):
            return
        if chat.pick(GO_ON.pattern, required=False):
            continue
        if len(chat.items) == 1:  # tela de "ok" com nome diferente
            chat.send(chat.items[0])
            continue
        raise FlowError("nao cheguei no menu — opcoes: %r" % (chat.items,))
    raise FlowError("nao cheguei no menu depois de %d telas" % max_hops)


def collect(cpf):
    """Percorre o fluxo e devolve o relatorio em texto."""
    chat = Chat()
    sections = []

    chat.pick(r"paciente")

    if chat.input_type != "text input":
        raise FlowError("esperava campo de CPF, veio %r" % (chat.input_type,))
    chat.send(cpf)

    reach_menu(chat)
    sections.append(("MENU", "", chat.items))

    # --- flores
    chat.pick(r"flor")
    sections.append(("FLORES", chat.text, chat.items))
    chat.pick(r"voltar|in[íi]cio", required=False)

    # --- concentrados
    if any(re.search(r"concentrad", i, re.I) for i in chat.items):
        chat.pick(r"concentrad")
        sections.append(("CONCENTRADOS", chat.text, chat.items))

        # o oleo costuma estar pendurado nesse ramo
        if chat.pick(r"[óo]leo", required=False):
            sections.append(("OLEO", chat.text, chat.items))

    return render(sections)


def render(sections):
    out = []
    for title, text, options in sections:
        out.append("== %s ==" % title)
        if text:
            out.append(text)
        if options:
            out.append("opcoes: " + " | ".join(options))
        out.append("")
    return tidy("\n".join(out))


# --------------------------------------------------------------- estado


def load_config(path):
    """Le config.ini. Ausente ou malformado nao e erro: caimos no ambiente."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not parser.has_section("abecmed"):
        return {}
    return {k: v.strip() for k, v in parser["abecmed"].items()}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, report):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"report": report, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            fh,
            ensure_ascii=False,
            indent=1,
        )
        fh.write("\n")


def added_lines(old, new):
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), n=0, lineterm="")
    return [
        ln[1:].strip()
        for ln in diff
        if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()
    ]


# ------------------------------------------------------------ notificacao


def notify(topic, title, message, tags):
    """ntfy e outro servico: nao herda o disfarce de navegador."""
    payload = {
        "topic": topic,
        "title": title,
        "message": message[:MAX_MSG],
        "tags": tags,
    }
    post_json(
        NTFY_BASE,
        payload,
        headers={
            "User-Agent": "abecmed-watch",
            "Origin": None,
            "Referer": None,
            "Sec-Fetch-Dest": None,
            "Sec-Fetch-Mode": None,
            "Sec-Fetch-Site": None,
        },
    )


# ------------------------------------------------------------------ main


def ciclo(cpf, topic, state_path, avisou_quebra):
    """Um ciclo completo. Devolve o novo valor de avisou_quebra."""
    try:
        report = collect(cpf)
    except FlowError as exc:
        print("FALHA: %s" % exc, file=sys.stderr)
        # So avisa na primeira falha da sequencia: senao vira spam a cada 9 min.
        if topic and not avisou_quebra:
            notify(
                topic,
                "abecmed-watch quebrou",
                "O fluxo do bot mudou e o script nao conseguiu navegar.\n\n%s" % exc,
                ["warning"],
            )
        return True

    if avisou_quebra:
        print("voltou a funcionar")
        if topic:
            notify(topic, "abecmed-watch voltou", "Navegacao normalizada.", ["white_check_mark"])

    state = load_state(state_path)
    previous = state.get("report", "")

    if report == previous:
        print("%s  sem mudancas" % time.strftime("%H:%M"))
        return False

    if not previous:
        title = "abecmed-watch ativo"
        body = "Primeira execucao. Catalogo atual:\n\n%s" % report
        tags = ["seedling"]
    else:
        novos = added_lines(previous, report)
        destaque = "Novidades:\n" + "\n".join(novos) + "\n\n" if novos else ""
        title = "ABECMED mudou"
        body = "%s%s" % (destaque, report)
        tags = ["bell"]

    notify(topic, title, body, tags)
    save_state(state_path, report)
    print("%s  mudanca detectada, notificacao enviada" % time.strftime("%H:%M"))
    return False


def main():
    parser = argparse.ArgumentParser(description="Monitor do catalogo da ABECMED")
    parser.add_argument(
        "--config", default=os.path.join(HERE, "config.ini"), help="arquivo de configuracao"
    )
    parser.add_argument(
        "--state", default=os.path.join(HERE, "estado.json"), help="arquivo de estado"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="imprime o relatorio, nao notifica nem salva"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="fica rodando, com intervalo sorteado entre cada verificacao",
    )
    args = parser.parse_args()

    # Ambiente ganha do arquivo: e assim que o GitHub Actions injeta os secrets.
    config = load_config(args.config)
    cpf = re.sub(r"\D", "", os.environ.get("ABECMED_CPF") or config.get("cpf", ""))
    topic = (os.environ.get("NTFY_TOPIC") or config.get("topico", "")).strip()

    def minutos(chave, padrao):
        try:
            return max(1, int(config.get(chave, padrao)))
        except (TypeError, ValueError):
            return padrao

    lo = minutos("intervalo_min", INTERVALO_MIN)
    hi = minutos("intervalo_max", INTERVALO_MAX)
    if hi < lo:
        lo, hi = hi, lo

    if not cpf:
        sys.exit(
            "erro: CPF nao configurado.\n"
            "  Rode ./configurar.sh (Linux/macOS) ou configurar.bat (Windows)."
        )
    if len(cpf) != 11:
        sys.exit("erro: CPF tem %d digitos, esperava 11. Rode o configurar de novo." % len(cpf))
    if not topic and not args.dry_run:
        sys.exit(
            "erro: topico do ntfy nao configurado.\n"
            "  Rode ./configurar.sh (Linux/macOS) ou configurar.bat (Windows),\n"
            "  ou use --dry-run para so testar sem notificar."
        )

    if args.dry_run:
        try:
            print(collect(cpf))
        except FlowError as exc:
            sys.exit("FALHA: %s" % exc)
        return

    if not args.loop:
        quebrou = ciclo(cpf, topic, args.state, False)
        sys.exit(1 if quebrou else 0)

    print("Monitorando. Intervalo sorteado entre %d e %d minutos." % (lo, hi))
    print("Para parar, aperte Ctrl+C.\n")
    avisou_quebra = False
    try:
        while True:
            avisou_quebra = ciclo(cpf, topic, args.state, avisou_quebra)
            espera = random.uniform(lo * 60, hi * 60)
            print("   proxima verificacao em %d min %02d s" % divmod(int(espera), 60)[0:2])
            time.sleep(espera)
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
