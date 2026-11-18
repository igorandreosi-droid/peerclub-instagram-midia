"""Publica no Instagram os posts da fila (pasta fila/) cuja hora chegou. Roda no GitHub Actions, só com a biblioteca padrão.

Cada pedido é um fila/<id>.json criado pelo `ig.py agendar` no computador da empresa (mídia já hospedada neste repositório
e já testada no Instagram). O resultado vai para fila/feito/<id>.json, e a existência dele impede publicar duas vezes.

Segredos do repositório: IG_TOKEN, IG_USER_ID e IG_HOST (graph.facebook.com ou graph.instagram.com).
TRAVA: o token tem que ser da conta escrita no pedido ("conta"); se não for, nada sai.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSAO = os.environ.get("IG_API_VERSION", "v23.0")
FILA = Path("fila")
FEITO = FILA / "feito"
ATRASO_MAXIMO = timedelta(hours=12)   # mais atrasado que isso não publica sozinho (avisa)
MAX_TENTATIVAS = 3


class ErroAPI(Exception):
    pass


def chamar(metodo, url, campos):
    dados = None
    cod = urllib.parse.urlencode(campos)
    if metodo == "GET":
        url = f"{url}?{cod}"
    else:
        dados = cod.encode()
    req = urllib.request.Request(url, data=dados, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        texto = e.read().decode(errors="replace")
        try:
            erro = json.loads(texto).get("error", {})
            texto = f"{erro.get('message')} (code {erro.get('code')}, subcode {erro.get('error_subcode')})"
        except ValueError:
            pass
        raise ErroAPI(f"HTTP {e.code} em {url.split('?')[0].split('/' + VERSAO)[-1]}: {texto}")


class IG:
    def __init__(self):
        self.token = os.environ["IG_TOKEN"].strip()
        self.uid = os.environ["IG_USER_ID"].strip()
        self.base = f"https://{os.environ.get('IG_HOST', 'graph.facebook.com').strip()}/{VERSAO}"

    def get(self, caminho, **campos):
        return chamar("GET", f"{self.base}/{caminho}", {**campos, "access_token": self.token})

    def post(self, caminho, **campos):
        return chamar("POST", f"{self.base}/{caminho}", {**campos, "access_token": self.token})

    def usuario(self):
        return (self.get(self.uid, fields="username").get("username") or "").lower()

    def container(self, **campos):
        return self.post(f"{self.uid}/media", **campos)["id"]

    def esperar(self, cid, limite_s=900):
        fim = time.time() + limite_s
        while time.time() < fim:
            r = self.get(cid, fields="status_code,status")
            if r.get("status_code") == "FINISHED":
                return
            if r.get("status_code") in ("ERROR", "EXPIRED"):
                raise ErroAPI(f"o Instagram recusou a mídia {cid}: {r.get('status')}")
            time.sleep(15)
        raise ErroAPI(f"a mídia {cid} não ficou pronta em {limite_s} s")

    def publicar(self, cid):
        mid = self.post(f"{self.uid}/media_publish", creation_id=cid)["id"]
        link = self.get(mid, fields="permalink").get("permalink")
        return mid, link


def colab(p):
    return {"collaborators": json.dumps(p["colaboradores"])} if p.get("colaboradores") else {}


def publicar_feed(ig, p):
    tipo = p["tipo"]
    if tipo == "carrossel":
        filhos = [ig.container(image_url=u, is_carousel_item="true") for u in p["midias"]]
        for f in filhos:
            ig.esperar(f)
        cid = ig.container(media_type="CAROUSEL", children=",".join(filhos), caption=p.get("legenda", ""), **colab(p))
    elif tipo == "imagem":
        cid = ig.container(image_url=p["midias"][0], caption=p.get("legenda", ""), **colab(p))
    elif tipo == "reels":
        extra = {"cover_url": p["capa_url"]} if p.get("capa_url") else {}
        cid = ig.container(media_type="REELS", video_url=p["midias"][0], caption=p.get("legenda", ""),
                           share_to_feed="true", **extra, **colab(p))
    else:
        raise ErroAPI(f"tipo desconhecido: {tipo}")
    ig.esperar(cid)
    return ig.publicar(cid)


def publicar_story(ig, s):
    campos = {"media_type": "STORIES"}
    campos["video_url" if s.get("tipo") == "video" else "image_url"] = s["midia"]
    if s.get("marcar"):
        campos["user_tags"] = json.dumps([{"username": u.lstrip("@")} for u in s["marcar"]])
    cid = ig.container(**campos)
    ig.esperar(cid)
    return ig.publicar(cid)


def gravar(nome, dados):
    FEITO.mkdir(parents=True, exist_ok=True)
    (FEITO / nome).write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    agora = datetime.now(timezone.utc)
    pedidos = sorted(FILA.glob("*.json"))
    vencidos = []
    for arq in pedidos:
        p = json.loads(arq.read_text(encoding="utf-8"))
        feito = FEITO / arq.name
        anterior = json.loads(feito.read_text(encoding="utf-8")) if feito.exists() else None
        if anterior and anterior.get("status") != "erro_temporario":
            continue
        quando = datetime.fromisoformat(p["publicar_em"])
        if quando > agora:
            print(f"aguardando: {arq.name} ({p['publicar_em']})")
            continue
        vencidos.append((arq, p, anterior))
    if not vencidos:
        print("nada vencido na fila")
        return
    ig = IG()
    conta = ig.usuario()
    for arq, p, anterior in vencidos:
        tentativas = (anterior or {}).get("tentativas", 0) + 1
        base = {"id": arq.stem, "publicar_em": p["publicar_em"], "tipo": p["tipo"], "serie": p.get("serie"),
                "tentativas": tentativas, "quando": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if conta != p["conta"].lstrip("@").lower():
            gravar(arq.name, {**base, "status": "erro", "erro": f"token é de @{conta}, pedido é de @{p['conta']}; nada publicado"})
            print(f"TRAVA: {arq.name} não publicado (conta errada)")
            continue
        if agora - datetime.fromisoformat(p["publicar_em"]) > ATRASO_MAXIMO:
            gravar(arq.name, {**base, "status": "atrasado", "erro": "passou de 12 h do horário; não publicado sozinho"})
            print(f"ATRASADO: {arq.name} não publicado")
            continue
        try:
            mid, link = publicar_feed(ig, p)
        except ErroAPI as e:
            status = "erro_temporario" if tentativas < MAX_TENTATIVAS else "erro"
            gravar(arq.name, {**base, "status": status, "erro": str(e)})
            print(f"ERRO ({status}): {arq.name}: {e}")
            continue
        resultado = {**base, "status": "publicado", "media_id": mid, "link": link,
                     "colaboradores": p.get("colaboradores", []), "midias": p["midias"]}
        gravar(arq.name, resultado)   # grava já: se o story falhar, o feed não sai de novo
        print(f"PUBLICADO: {arq.name} {link}")
        if p.get("story"):
            try:
                smid, slink = publicar_story(ig, p["story"])
                resultado["story"] = {"media_id": smid, "link": slink, "marcados": p["story"].get("marcar", [])}
                print(f"STORY: {slink}")
            except ErroAPI as e:
                resultado["story"] = {"erro": str(e)}
                print(f"STORY com erro: {e}")
            gravar(arq.name, resultado)


if __name__ == "__main__":
    try:
        if os.environ.get("SO_VERIFICAR") == "1":   # confere o token sem publicar nada
            print(f"TOKEN OK: conta @{IG().usuario()}")
            sys.exit(0)
        main()
    except KeyError as e:
        sys.exit(f"falta o segredo {e} no repositório (Settings > Secrets and variables > Actions)")
