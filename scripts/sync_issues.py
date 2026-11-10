"""Copy the source repo's issues and PR descriptions (with comments) into the
backup as issues titled "[원본 이슈 #N] …" / "[원본 PR #N] …".

New ones are created; a copy whose source changed (title, body, comments or
state) is updated. GitHub keeps each issue's edit history, so an earlier
version of a copied text can still be read there. Copies are never deleted,
so an issue removed from the source stays in the backup.
Needs: gh CLI, GH_TOKEN with issues: write on DST.
"""
import json
import os
import re
import subprocess

SRC = os.environ["SRC_REPO"]
DST = os.environ["DST_REPO"]
DRY = os.environ.get("DRY_RUN") == "1"
TITLE = re.compile(r"^\[원본 (?:이슈|PR) #(\d+)\]")


def gh(*args, data=None, ok=False):
    if DRY and "-X" in args:
        print("  (dry run) would call:", " ".join(args[:4]))
        return '{"number": 0}'
    r = subprocess.run(["gh", *args], input=data, capture_output=True, text=True)
    if r.returncode != 0 and not ok:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


def api_all(path):
    # --slurp wraps every page in one JSON array, so bodies containing "]["
    # can never break the split between pages.
    pages = json.loads(gh("api", "--paginate", "--slurp", path))
    items = []
    for page in pages:
        items.extend(page if isinstance(page, list) else [page])
    return items


def ensure_labels():
    have = {l["name"] for l in api_all(f"repos/{DST}/labels?per_page=100")}
    for l in api_all(f"repos/{SRC}/labels?per_page=100"):
        if l["name"] in have:
            continue
        payload = {"name": l["name"], "color": l["color"], "description": l.get("description") or ""}
        gh("api", "-X", "POST", f"repos/{DST}/labels", "--input", "-", data=json.dumps(payload), ok=True)


def render(it, comments):
    n = it["number"]
    is_pr = "pull_request" in it
    kind = "PR" if is_pr else "이슈"
    meta = [
        f"> 원본: {it['html_url']}",
        f"> 종류: {kind} · 작성자: @{it['user']['login']} · 작성: {it['created_at'][:10]} · 상태: {it['state']}",
    ]
    if is_pr:
        pr = json.loads(gh("api", f"repos/{SRC}/pulls/{n}"))
        merged = pr.get("merged_at")
        meta.append(
            f"> 브랜치: `{pr['head']['ref']}` → `{pr['base']['ref']}` · "
            + (f"병합: {merged[:10]}" if merged else "병합 안 됨")
            + f" · 백업 브랜치: `pr/{n}`"
        )
    body = "\n".join(meta).replace("@", "@​") + "\n\n---\n\n" + (it.get("body") or "_(본문 없음)_")
    for c in sorted(comments.get(n, []), key=lambda c: c["created_at"]):
        where = f" (`{c['path']}`)" if c.get("path") else ""
        body += f"\n\n---\n**댓글** {c['user']['login']} · {c['created_at'][:10]}{where}\n\n{c.get('body') or ''}"
    return f"[원본 {kind} #{n}] {it['title']}", body


def main():
    ensure_labels()
    comments = {}
    for c in api_all(f"repos/{SRC}/issues/comments?per_page=100"):
        comments.setdefault(int(c["issue_url"].rsplit("/", 1)[-1]), []).append(c)
    for c in api_all(f"repos/{SRC}/pulls/comments?per_page=100"):
        comments.setdefault(int(c["pull_request_url"].rsplit("/", 1)[-1]), []).append(c)

    copies = {}
    for it in api_all(f"repos/{DST}/issues?state=all&per_page=100"):
        if "pull_request" in it:
            continue
        m = TITLE.match(it["title"])
        if m:
            copies[int(m.group(1))] = it

    created = updated = 0
    for it in sorted(api_all(f"repos/{SRC}/issues?state=all&per_page=100"), key=lambda i: i["number"]):
        n = it["number"]
        title, body = render(it, comments)
        state = "closed" if it["state"] == "closed" else "open"
        labels = [l["name"] for l in it.get("labels", [])]
        copy = copies.get(n)
        if copy is None:
            new = json.loads(gh("api", "-X", "POST", f"repos/{DST}/issues", "--input", "-",
                                data=json.dumps({"title": title, "body": body, "labels": labels})))
            if state == "closed":
                gh("api", "-X", "PATCH", f"repos/{DST}/issues/{new['number']}", "--input", "-",
                   data=json.dumps({"state": "closed"}))
            created += 1
            print(f"created #{n} -> #{new['number']}")
            continue
        if copy["title"] != title or (copy.get("body") or "") != body or copy["state"] != state:
            gh("api", "-X", "PATCH", f"repos/{DST}/issues/{copy['number']}", "--input", "-",
               data=json.dumps({"title": title, "body": body, "state": state, "labels": labels}))
            updated += 1
            print(f"updated #{n} -> #{copy['number']}")
    print(f"issues: {created} created, {updated} updated")


if __name__ == "__main__":
    main()
