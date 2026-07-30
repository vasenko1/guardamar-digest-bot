from __future__ import annotations
import argparse, json
from urllib.request import Request, urlopen
from .config import settings
from .importer import import_export
from .dedupe import dedupe
from .llm import classify
from .render import render

def send(token, chat_id, text):
    req=Request(f"https://api.telegram.org/bot{token}/sendMessage", data=json.dumps({"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}).encode(), headers={"Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=40) as r: return json.loads(r.read())

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd", required=True)
    x=sub.add_parser("import"); x.add_argument("file"); x.add_argument("--period", required=True)
    x=sub.add_parser("dedupe"); x.add_argument("--period", required=True)
    x=sub.add_parser("classify"); x.add_argument("--period", required=True)
    x=sub.add_parser("preview"); x.add_argument("--period", required=True); x.add_argument("--send", action="store_true")
    x=sub.add_parser("publish"); x.add_argument("--period", required=True)
    a=p.parse_args(); s=settings()
    if a.cmd=="import": print(import_export(s.db_path, __import__('pathlib').Path(a.file), a.period, s.source_chat_id, s.source_username))
    elif a.cmd=="dedupe": print(json.dumps(dedupe(s.db_path, a.period), ensure_ascii=False))
    elif a.cmd=="classify": print(classify(s,a.period))
    else:
        parts=render(s,a.period)
        target=s.admin_chat_id if a.cmd=="preview" else s.source_chat_id
        if not (a.cmd=="preview" and not a.send):
            if not s.bot_token or not target: raise SystemExit("Bot token and target chat ID are required")
            for part in parts: send(s.bot_token,target,part)
        else: print("\n\n--- PART ---\n".join(parts))

if __name__ == "__main__": main()
