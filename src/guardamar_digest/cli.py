from __future__ import annotations
import argparse, json
from urllib.request import Request, urlopen
from .config import settings
from .importer import import_export
from .dedupe import dedupe, semantic_dedupe, review_report
from .dedupe import decide_pairs, exclude_messages
from .llm import classify
from .render import render

def send(token, chat_id, text):
    req=Request(f"https://api.telegram.org/bot{token}/sendMessage", data=json.dumps({"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}).encode(), headers={"Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=40) as r: return json.loads(r.read())

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="cmd", required=True)
    x=sub.add_parser("import"); x.add_argument("file"); x.add_argument("--period", required=True)
    x=sub.add_parser("dedupe"); x.add_argument("--period", required=True); x.add_argument("--semantic", action="store_true")
    x=sub.add_parser("duplicate-review"); x.add_argument("--period", required=True)
    x=sub.add_parser("duplicate-decide"); x.add_argument("--period", required=True)
    choice=x.add_mutually_exclusive_group(required=True); choice.add_argument("--same", nargs="+"); choice.add_argument("--different", nargs="+")
    x=sub.add_parser("exclude"); x.add_argument("--period", required=True); x.add_argument("--reason", required=True); x.add_argument("message_ids", nargs="+", type=int)
    x=sub.add_parser("classify"); x.add_argument("--period", required=True)
    x=sub.add_parser("preview"); x.add_argument("--period", required=True); x.add_argument("--send", action="store_true")
    x=sub.add_parser("publish"); x.add_argument("--period", required=True)
    a=p.parse_args(); s=settings()
    if a.cmd=="import": print(import_export(s.db_path, __import__('pathlib').Path(a.file), a.period, s.source_chat_id, s.source_username))
    elif a.cmd=="dedupe":
        result=dedupe(s.db_path, a.period)
        if a.semantic: result["semantic"] = semantic_dedupe(s, a.period)
        print(json.dumps(result, ensure_ascii=False))
    elif a.cmd=="duplicate-review": print(review_report(s.db_path, a.period))
    elif a.cmd=="duplicate-decide":
        values=a.same if a.same is not None else a.different
        pairs=[]
        for value in values:
            try:
                left, right = (int(part) for part in value.split(":", 1))
            except ValueError:
                raise SystemExit(f"Invalid pair {value!r}; use MESSAGE_ID:MESSAGE_ID")
            pairs.append((left, right))
        print(decide_pairs(s.db_path, a.period, pairs, same=a.same is not None))
    elif a.cmd=="exclude": print(exclude_messages(s.db_path, a.period, a.message_ids, a.reason))
    elif a.cmd=="classify": print(classify(s,a.period))
    else:
        parts=render(s,a.period)
        target=s.admin_chat_id if a.cmd=="preview" else s.source_chat_id
        if not (a.cmd=="preview" and not a.send):
            if not s.bot_token or not target: raise SystemExit("Bot token and target chat ID are required")
            for part in parts: send(s.bot_token,target,part)
        else: print("\n\n--- PART ---\n".join(parts))

if __name__ == "__main__": main()
