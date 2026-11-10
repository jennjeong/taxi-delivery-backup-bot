"""Build and send one Telegram message per source push.

Runs after scripts/mirror.sh. Reads the ref changes it recorded
(TRANSITIONS_FILE) and the repository it fetched (WORK_DIR), then for every
push between the previous backup and now:

- lists new commits (max 3),
- on an overwrite (force push / history rewrite), finds commits that left the
  branch history and checks, per commit, whether the same change is still in
  the branch (rebase, squash) or the file content really went back,
- on any push, looks for proof that someone else's change was undone: a commit
  that restores a file to an exact earlier version, or a merge that kept one
  side of a file and dropped the other side's change,
- reports what the backup really holds: the new state, and the state before
  an overwrite (with a link only when the backup ref was verified).

Push details (pusher, time, push type) come from the source repository's
activity API. Without it, one message per changed ref is sent and unknown
values are left out.

Env: WORK_DIR, TRANSITIONS_FILE, SRC_REPO, DST_REPO, SRC_URL, DST_URL, STAMP.
Optional: ACTIVITY_FILE (JSON instead of the API), NO_API=1, DRY_RUN=1,
ISSUES_SUMMARY_FILE, MIRROR_OUTCOME, ISSUES_OUTCOME, TEST_MESSAGE,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RUN_URL.
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ENV = os.environ
GIT_DIR = ENV.get("WORK_DIR", "")
SRC = ENV.get("SRC_REPO", "")
DST = ENV.get("DST_REPO", "")
SRC_URL = ENV.get("SRC_URL", "")
DST_URL = ENV.get("DST_URL", "")
STAMP = ENV.get("STAMP", dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ"))
WEB = "https://github.com"
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SHOW = 3


# ---------------------------------------------------------------- git helpers
def git(*args, data=None, ok=False, env=None):
    r = subprocess.run(["git", f"--git-dir={GIT_DIR}", *args], input=data,
                       capture_output=True, text=data is None or isinstance(data, str),
                       env={**ENV, **(env or {})})
    if r.returncode != 0:
        if ok:
            return None
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout.strip() if isinstance(r.stdout, str) else r.stdout


def have(sha):
    return bool(sha) and git("cat-file", "-e", f"{sha}^{{commit}}", ok=True) is not None


def obtain(sha):
    """True when the commit is readable here, fetching it by SHA if needed."""
    if have(sha):
        return True
    for url in (SRC_URL, DST_URL):
        if url:
            git("fetch", "-q", url, sha, ok=True)
            if have(sha):
                return True
    return False


def ancestor(a, b):
    r = subprocess.run(["git", f"--git-dir={GIT_DIR}", "merge-base", "--is-ancestor", a, b])
    return r.returncode == 0


def blob(rev, path):
    return git("rev-parse", "-q", "--verify", f"{rev}:{path}", ok=True)


def lines(out):
    return [l for l in (out or "").splitlines() if l]


def info(sha):
    f = git("show", "-s", "--format=%H%x00%an%x00%ae%x00%s%x00%P", sha).split("\0")
    return {"sha": f[0], "name": f[1], "email": f[2], "subject": f[3],
            "parents": f[4].split()}


_logins = {}


def login(sha):
    """GitHub login of a commit's author, or None."""
    if ENV.get("NO_API") == "1" or not SRC:
        return None
    if sha not in _logins:
        r = subprocess.run(["gh", "api", f"repos/{SRC}/commits/{sha}", "--jq", ".author.login // empty"],
                           capture_output=True, text=True)
        _logins[sha] = r.stdout.strip() or None if r.returncode == 0 else None
    return _logins[sha]


def author(c):
    c["login"] = login(c["sha"])
    c["who"] = c["login"] or c["name"]
    return c


def same_person(c, ref):
    """ref: {"login": .., "name": .., "email": ..} of the pusher (or tip author)."""
    if not ref:
        return True
    if c.get("login") and ref.get("login"):
        return c["login"].lower() == ref["login"].lower()
    keys = {v.lower() for v in (ref.get("login"), ref.get("name")) if v}
    email = (c.get("email") or "").lower()
    return (c["name"].lower() in keys or (c.get("login") or "").lower() in keys
            or any(email.startswith(k + "@") or f"+{k}@" in email for k in keys)
            or (ref.get("email") and email == ref["email"].lower()))


# --------------------------------------------------------- content checks
def applies_reverse(patch, rev):
    """True when `patch` is already contained in `rev` (it un-applies cleanly)."""
    if not patch.strip():
        return True
    with tempfile.TemporaryDirectory() as tmp:
        idx = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        git("read-tree", rev, env=idx)
        r = subprocess.run(["git", f"--git-dir={GIT_DIR}", "apply", "--cached", "--check", "-R"],
                           input=patch, capture_output=True, env={**ENV, **idx})
        return r.returncode == 0


def diff(a, b, *paths):
    return git("diff", "--binary", "--full-index", a, b, "--", *paths).encode() + b"\n"


def classify_dropped(c, before, after, base, equivalent):
    """How a commit that left the branch history stands in `after`:
    kept (same change still there: rebase, squash), gone (file content
    proven back to before the commit) or dropped (only left the history)."""
    if c["sha"] in equivalent:
        return "kept", None
    parent = c["parents"][0] if c["parents"] else EMPTY_TREE
    if applies_reverse(diff(parent, c["sha"]), after):
        return "kept", None
    rows = [r.split("\t", 1) for r in lines(git("diff-tree", "--no-commit-id", "-r", "--name-status", parent, c["sha"]))]
    paths = [p.split("\t")[-1] for _, p in rows]
    # A later commit on the old branch may have built on this one (then
    # squashed): the old branch's final change to these files is still there.
    if paths and applies_reverse(diff(base, before, *paths), after):
        return "kept", None
    gone = []
    for status, path in rows:
        path = path.split("\t")[-1]
        now = blob(after, path)
        if status.startswith("A") and now is None:
            gone.append(f"{path} 없음")
        elif status.startswith("M") and now is not None and now == blob(parent, path):
            gone.append(f"{path} 이전 내용으로 돌아감")
    return ("gone", gone) if gone else ("dropped", None)


def undone_by_restore(c, pusher):
    """Files that commit c set back to an exact earlier version, undoing
    other people's commits in between."""
    found = []
    if len(c["parents"]) != 1:
        return found
    for row in lines(git("diff-tree", "--no-commit-id", "-r", "--name-status", c["parents"][0], c["sha"])):
        status, path = row.split("\t", 1)
        if status != "M":
            continue
        target = blob(c["sha"], path)
        history = lines(git("log", "--format=%H", "-n", "25", c["parents"][0], "--", path))
        for k, x in enumerate(history[1:], start=1):
            if blob(x, path) != target:
                continue
            lost = [author(info(h)) for h in history[:k]]
            lost = [l for l in lost if len(l["parents"]) == 1
                    and not same_person(l, pusher) and not same_person(l, c)]
            if lost:
                found.append({"path": path, "by": c, "kind": "restore", "base": x, "lost": lost})
            break
    return found


def undone_by_merge(c, pusher):
    """Files where merge c kept one side and dropped the other side's change."""
    found = []
    if len(c["parents"]) != 2:
        return found
    p1, p2 = c["parents"]
    base = git("merge-base", p1, p2, ok=True)
    if not base:
        return found
    for side, other in ((p2, p1), (p1, p2)):
        for path in lines(git("diff", "--name-only", base, side)):
            m, s, o, b = blob(c["sha"], path), blob(side, path), blob(other, path), blob(base, path)
            if s is None or m is None or m != o or o == s or s == b:
                continue
            if applies_reverse(diff(base, side, path), c["sha"]):
                continue
            lost = [author(info(h)) for h in lines(git("rev-list", "--no-merges", f"{base}..{side}", "--", path))]
            lost = [l for l in lost if not same_person(l, pusher) and not same_person(l, c)]
            if lost:
                found.append({"path": path, "by": c, "kind": "merge", "lost": lost})
    return found


# ------------------------------------------------------------- input
def read_transitions(path):
    changes, pushed, archived = [], {}, {}
    if not path or not os.path.exists(path):
        return changes, pushed, archived
    for row in open(path, encoding="utf-8"):
        f = row.rstrip("\n").split("\t")
        if f[0] == "change":
            _, src_ref, name, old, new, archive, what = f
            changes.append({"src_ref": src_ref, "name": name, "old": None if old == "-" else old,
                            "new": None if new == "-" else new,
                            "archive": None if archive == "-" else archive, "what": what})
        elif f[0] == "pushed":
            pushed[f[1].removeprefix("refs/")] = f[2]
        elif f[0] == "archived":
            archived[f[1]] = (f[2], f[3])
    return changes, pushed, archived


def read_activity():
    if ENV.get("ACTIVITY_FILE"):
        return json.load(open(ENV["ACTIVITY_FILE"], encoding="utf-8"))
    if ENV.get("NO_API") == "1":
        return []
    r = subprocess.run(["gh", "api", f"repos/{SRC}/activity?per_page=100"], capture_output=True, text=True)
    if r.returncode != 0:
        print("activity API unavailable:", r.stderr.strip()[:200])
        return []
    return json.loads(r.stdout)


def chain(ref, old, new, activity):
    """Pushes on `ref` that lead from old to new (new None: deleted), oldest
    first, or None when the activity does not connect them."""
    entries = [a for a in activity if a.get("ref") == ref]
    out, cur, used = [], new or ZERO, set()
    for _ in range(30):
        e = next((a for a in entries if a["after"] == cur and id(a) not in used), None)
        if e is None:
            return None
        used.add(id(e))
        out.append(e)
        before = None if e["before"] == ZERO else e["before"]
        if before == old:
            return list(reversed(out))
        if before is None:
            return None
        cur = before
    return None


# ------------------------------------------------------------- backup state
dst_refs = {}
_verified = {}


def load_dst_refs():
    dst_refs.clear()
    out = subprocess.run(["git", f"--git-dir={GIT_DIR}", "ls-remote", DST_URL],
                         capture_output=True, text=True).stdout if DST_URL else ""
    for row in lines(out):
        sha, ref = row.split("\t")
        dst_refs[ref.removeprefix("refs/")] = sha


def opens(ref, sha, paths=()):
    """Read `ref` back from the backup itself and check it holds commit `sha`
    and every path in `paths` at that commit. Returns the readable paths, or
    None when the commit cannot be read from the backup."""
    if not DST_URL:
        return None
    key = f"refs/verify/{len(_verified)}"
    if ref not in _verified:
        ok = git("fetch", "-q", DST_URL, f"+refs/{ref}:{key}", ok=True) is not None
        _verified[ref] = key if ok else None
    key = _verified[ref]
    if not key:
        return None
    tip = git("rev-parse", key)
    if tip != sha and not ancestor(sha, tip):
        return None
    return [p for p in paths if git("cat-file", "-e", f"{sha}:{p}", ok=True) is not None]


def recovery(sha, short, paths=(), preferred=None):
    """A backup ref from which commit `sha` (and `paths`) really opens:
    {"ref", "link", "files"} or None. Keeps `sha` as a new archive ref first
    when no backup ref holds it."""
    candidates = []
    if preferred and dst_refs.get(preferred) == sha:
        candidates.append(preferred)
    candidates += [r for r, t in dst_refs.items() if t == sha and r.startswith(("heads/", "tags/"))]
    candidates += [r for r, t in dst_refs.items() if r.startswith(("heads/", "tags/")) and t != sha
                   and have(t) and ancestor(sha, t)]
    if not candidates and DST_URL and have(sha):
        name = f"heads/archive/{short}/{STAMP}-{sha[:7]}"
        if git("push", "-q", DST_URL, f"{sha}:refs/{name}", ok=True) is not None:
            load_dst_refs()
            if dst_refs.get(name) == sha:
                candidates.append(name)
    for ref in dict.fromkeys(candidates):
        files = opens(ref, sha, paths)
        if files is None:
            continue
        at = ref.split("/", 1)[1] if dst_refs.get(ref) == sha else sha
        return {"ref": ref.split("/", 1)[1], "sha": sha, "at": at,
                "link": f"{WEB}/{DST}/tree/{quote(at)}", "files": files}
    return None


def quote(path):
    return urllib.parse.quote(path, safe="/")


def describe_diff(have_sha, want_sha):
    """Files that differ between what the backup has and the source."""
    if not want_sha or not have(want_sha):
        return []
    if not have_sha or not have(have_sha):
        return ["백업에 이 브랜치가 없음"]
    names = {"A": "없음", "D": "백업에만 있음", "M": "내용 다름", "T": "종류 다름"}
    out = []
    for row in lines(git("diff", "--raw", "--no-renames", have_sha, want_sha)):
        meta, path = row.split("\t", 1)
        f = meta.split()
        status = f[4][0]
        if status == "M" and f[0] != f[1] and f[2] == f[3]:
            out.append(f"{path}: 실행 권한 다름")
        else:
            out.append(f"{path}: {names.get(status, status)}")
    return out


def audit():
    """Compare every source branch and tag, and the default branch, with the
    backup after this run. Returns a list of problems (empty: all match)."""
    load_dst_refs()
    problems = []
    frozen = set(ENV.get("FROZEN", "main").split())
    for ref in lines(git("for-each-ref", "--format=%(refname)", "refs/src/heads", "refs/src/tags")):
        kind, name = ("heads", ref[len("refs/src/heads/"):]) if ref.startswith("refs/src/heads/") \
            else ("tags", ref[len("refs/src/tags/"):])
        dst = f"heads/source/{name}" if kind == "heads" and name in frozen else f"{kind}/{name}"
        want = git("rev-parse", ref)
        got = dst_refs.get(dst)
        if got == want:
            continue
        label = ("브랜치 " if kind == "heads" else "태그 ") + name
        detail = describe_diff(got, want)
        problems.append((label, f"원본 <code>{want[:7]}</code> · 백업 " + (f"<code>{got[:7]}</code>" if got else "없음"),
                         detail))
    if ENV.get("NO_API") != "1":
        def default(repo):
            r = subprocess.run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"], capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else None
        src_default, dst_default = default(SRC), default(DST)
        if src_default and dst_default:
            want = f"source/{src_default}" if src_default in frozen else src_default
            if dst_default != want:
                problems.append(("기본 브랜치", f"백업 기본 브랜치가 {E(dst_default)}예요. 원본 {E(src_default)}에 맞춰 {E(want)}여야 클론했을 때 원본 파일이 보여요.", []))
    return problems


# ------------------------------------------------------------- analysis
def kst(ts):
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=9)))
    return t.strftime("%Y-%m-%d %H:%M KST")


def pusher_of(push, sha):
    if push.get("pusher"):
        return {"login": push["pusher"]}
    tip = author(info(sha))
    return {"login": tip.get("login"), "name": tip["name"], "email": tip["email"]}


def default_tip():
    for name in ("main", "master"):
        sha = git("rev-parse", "-q", "--verify", f"refs/src/heads/{name}", ok=True)
        if sha:
            return sha
    return None


def analyse_deleted(push, change):
    """A branch or tag removed in the source."""
    before = push["before"]
    p = {**push, "deleted": True, "readable_before": have(before), "readable_after": False,
         "new_commits": [], "dropped": [], "undone": [], "notes": [], "removed_files": []}
    if not p["readable_before"]:
        return p
    pusher = pusher_of(push, before)
    others = [r for r in lines(git("for-each-ref", "--format=%(refname)", "refs/src/heads")) if r != change["src_ref"]]
    keep = default_tip()
    equivalent = {l[2:] for l in lines(git("cherry", keep, before)) if l.startswith("- ")} if keep else set()
    base = (git("merge-base", keep, before, ok=True) if keep else None) or EMPTY_TREE
    for s in lines(git("rev-list", "--no-merges", "-n", "50", before, "--not", *others)):
        c = author(info(s))
        c["state"], c["files"] = classify_dropped(c, before, keep, base, equivalent) if keep else ("dropped", None)
        c["other"] = not same_person(c, pusher)
        p["dropped"].append(c)
    p["recovery"] = recovery(before, change["name"].split("/", 1)[1], (), change["archive"])
    return p


def analyse(push, change, pushed, archived):
    before, after = push["before"], push["after"]
    if after is None:
        return analyse_deleted(push, change)
    readable_before = before is not None and obtain(before)
    readable_after = obtain(after)
    p = {**push, "readable_before": readable_before, "readable_after": readable_after,
         "new_commits": [], "dropped": [], "undone": [], "notes": [], "removed_files": []}
    short = change["name"].split("/", 1)[1]
    if not readable_after:
        p["notes"].append(f"이 푸시의 커밋 {after[:7]}을(를) 원본과 백업 어디서도 읽을 수 없어요. "
                          "다음 백업 전에 다시 덮어써진 것으로 보여요.")
        p["latest"] = ("missed", None)
        return p
    pusher = pusher_of(push, after)
    p["overwrite"] = push.get("type") == "force_push" or bool(readable_before and not ancestor(before, after))
    if readable_before:
        rng = [f"{before}..{after}"]
    else:
        # New branch, or the previous state cannot be read: commits that no
        # other source branch (nor the last backed-up state) has.
        others = [r for r in lines(git("for-each-ref", "--format=%(refname)", "refs/src/heads", "refs/src/tags"))
                  if r != change["src_ref"]]
        if change["old"] and have(change["old"]):
            others.append(change["old"])
        rng = [after, "--not", *others]
    shas = lines(git("rev-list", "-n", "100", *rng))
    p["new_total"] = len(lines(git("rev-list", *rng)))
    p["new_commits"] = [author(info(s)) for s in shas]
    for c in p["new_commits"]:
        p["undone"] += undone_by_restore(c, pusher) + undone_by_merge(c, pusher)
    if readable_before:
        p["removed_files"] = lines(git("diff", "--name-only", "--diff-filter=D", "--no-renames", before, after))
    if p["overwrite"] and readable_before:
        equivalent = {l[2:] for l in lines(git("cherry", after, before)) if l.startswith("- ")}
        base = git("merge-base", after, before, ok=True) or EMPTY_TREE
        for s in lines(git("rev-list", "--no-merges", "-n", "50", f"{after}..{before}")):
            c = author(info(s))
            c["state"], c["files"] = classify_dropped(c, before, after, base, equivalent)
            c["other"] = not same_person(c, pusher)
            p["dropped"].append(c)

    # 💾 latest state of this push in the backup.
    if after == change["new"]:
        status = pushed.get(change["name"])
        p["latest"] = ({"ok": "done", "held": "held", "refused": "failed"}.get(status, "unknown"),
                       change["name"].split("/", 1)[1] if status == "ok" else None)
        if status == "ok" and opens(change["name"], after) is None:
            p["latest"] = ("unknown", None)
        if status in ("held", "refused"):
            base = dst_refs.get(change["name"])
            p["missing"] = describe_diff(base, after)
    else:
        kept = recovery(after, short)
        p["latest"] = ("between", kept) if kept else ("unknown", None)

    # 🛟 the version this push hid (overwrite or removed files).
    if p["overwrite"] or p["removed_files"]:
        p["needs_recovery"] = True
        if readable_before:
            gone = p["removed_files"]
            preferred = change["archive"] if before == change["old"] else None
            p["recovery"] = recovery(before, short, gone[:SHOW], preferred)
    return p


# ------------------------------------------------------------- message
E = html.escape


def link(url, text):
    return f'<a href="{E(url, quote=True)}">{E(text)}</a>'


def cut(text, n=70):
    return text if len(text) <= n else text[: n - 1] + "…"


def commit_line(c, extra=""):
    return f"• <code>{c['sha'][:7]}</code> {E(cut(c['subject']))} — 작성자 {E(c['who'])}{extra}"


def message(p, label):
    lost_commits = [c for c in p["dropped"] if c["other"] and c["state"] != "kept"]
    lost_undone = p["undone"]
    deleted = p.get("deleted")
    unknown = p.get("overwrite") and not p["readable_before"]
    out = []
    if lost_commits or lost_undone:
        out.append("<b>⚠️ 변경 유실 주의</b>")
    elif unknown:
        out.append("<b>⚠️ 덮어쓰기 감지 · 유실 여부 확인 필요</b>")
    kind = ("브랜치 삭제" if label[0] == "브랜치" else "삭제") if deleted else \
        "강제 푸시" if p.get("overwrite") else "새 브랜치" if p["before"] is None else None
    out.append("<b>🔔 GitHub 원본 변경 감지</b>" + (f" ({kind})" if kind else ""))
    out.append(f"저장소: {E(SRC)}")
    out.append(f"{label[0]}: {E(label[1])}")
    if p.get("pusher"):
        out.append(f"푸시한 사람: {E(p['pusher'])}")
    if p.get("time"):
        out.append(f"시각: {E(kst(p['time']))}")

    if lost_commits:
        where = "원본 어느 브랜치에도 없음" if deleted else "현재 브랜치 이력에서 빠짐"
        out += ["", f"<b>다른 작성자 커밋이 빠짐</b> ({len(lost_commits)}개)"]
        for c in lost_commits[:SHOW]:
            if c["state"] == "gone" and not deleted:
                extra = " · 변경 내용도 현재 브랜치에 없음 (" + E(", ".join(c["files"][:2])) + ")"
            else:
                extra = f" · {where}"
            out.append(commit_line(c, extra))
        if len(lost_commits) > SHOW:
            out.append(f"외 {len(lost_commits) - SHOW}개")
    if lost_undone:
        out += ["", "<b>덮어쓴 변경</b> (비교로 확인됨)"]
        for u in lost_undone[:SHOW]:
            by = f"<code>{u['by']['sha'][:7]}</code>(작성자 {E(u['by']['who'])})"
            if u["kind"] == "restore":
                how = f"커밋 {by}에서 <code>{u['base'][:7]}</code> 시점 내용으로 되돌아감"
            else:
                how = f"병합 {by}에서 한쪽 내용만 남음"
            out.append(f"• {E(u['path'])}: {how}")
            for l in u["lost"][:2]:
                out.append(f"  빠진 변경: <code>{l['sha'][:7]}</code> {E(cut(l['subject'], 50))} — 작성자 {E(l['who'])}")
            if len(u["lost"]) > 2:
                out.append(f"  외 {len(u['lost']) - 2}개")
        if len(lost_undone) > SHOW:
            out.append(f"외 {len(lost_undone) - SHOW}개 파일")
    if p["readable_before"] and not lost_commits and (p.get("overwrite") or deleted):
        kept = [c for c in p["dropped"] if c["state"] == "kept"]
        own = [c for c in p["dropped"] if c["state"] != "kept"]
        if kept:
            where = "기본 브랜치에" if deleted else "현재 브랜치에"
            out.append(f"\n이력에서 빠진 커밋 {len(kept)}개는 해시만 바뀌고 같은 변경이 {where} 남아 있어요.")
        if own:
            out.append(f"이력에서 빠진 커밋 {len(own)}개는 모두 푸시한 사람의 커밋이에요.")
        if deleted and not p["dropped"]:
            out.append("\n이 브랜치의 커밋은 모두 원본의 다른 브랜치에 남아 있어요.")
    if unknown:
        out += ["", "<b>확인 범위</b>"]
        out.append(f"이전 상태 <code>{p['before'][:7]}</code>를 원본과 백업에서 읽지 못해 빠진 커밋과 파일을 비교하지 못했어요.")
        if p["readable_after"]:
            out.append(f"새 상태 <code>{p['after'][:7]}</code>와 다른 브랜치에 없는 새 커밋만 확인했어요.")
    if p["notes"]:
        out += [""] + [E(n) for n in p["notes"]]
    if p["removed_files"]:
        out += ["", f"<b>삭제된 파일</b> ({len(p['removed_files'])}개)"]
        out += [f"• {E(f)}" for f in p["removed_files"][:SHOW]]
        if len(p["removed_files"]) > SHOW:
            out.append(f"외 {len(p['removed_files']) - SHOW}개")

    if p["new_commits"]:
        out += ["", f"<b>새 커밋</b> ({p['new_total']}개)" if p["new_total"] > SHOW else "<b>새 커밋</b>"]
        shown = [c for c in p["new_commits"] if len(c["parents"]) < 2][:SHOW] or p["new_commits"][:SHOW]
        out += [commit_line(c) for c in shown]

    # 💾 latest state
    out += ["", "<b>💾 최신 파일 백업</b>"]
    if deleted:
        out.append("• 원본에서 지워졌어요. 백업의 같은 이름 브랜치는 지우지 않고 그대로 둬요.")
    else:
        state, ref = p.get("latest", ("unknown", None))
        if state == "done":
            out.append(f"• 완료: 백업 {link(f'{WEB}/{DST}/tree/{quote(ref)}', ref)}의 커밋과 파일이 원본과 같아요.")
        elif state == "held":
            out.append("• 보류: 이전 버전을 보관·검증하지 못해 기존 백업을 그대로 뒀어요. 다음 실행에서 다시 시도해요.")
        elif state == "failed":
            out.append("• 실패: 백업에 올리지 못했어요. 기존 백업은 그대로예요.")
        elif state == "between":
            out.append(f"• 완료: 다음 푸시 전 상태라 {link(ref['link'], ref['ref'])}에 따로 보관했어요.")
        elif state == "missed":
            out.append("• 반영 못함: 다음 백업 전에 덮어써져 이 상태를 가져오지 못했어요.")
        else:
            out.append("• 확인 못함")
        if state in ("held", "failed") and p.get("missing"):
            out.append("• 아직 백업에 없는 변경:")
            out += [f"  {E(m)}" for m in p["missing"][:5]]
            if len(p["missing"]) > 5:
                out.append(f"  외 {len(p['missing']) - 5}개")

    # 🛟 previous version
    if p.get("needs_recovery") or deleted:
        rec = p.get("recovery")
        if rec:
            out += ["", "<b>🛟 이전 버전 복구</b>"]
            out.append(f"• 복구 가능: {link(rec['link'], rec['ref'])} (커밋 <code>{rec['sha'][:7]}</code>, 백업에서 열리는 것 확인)")
            for f in rec["files"]:
                out.append(f"• 파일: {link(f'{WEB}/{DST}/blob/{quote(rec['at'])}/{quote(f)}', f)}")
        else:
            out += ["", "<b>⚠️ 이전 버전 복구 미확인</b>"]
            if p["before"] and not p["readable_before"]:
                out.append(f"• 원본에서 사라진 <code>{p['before'][:7]}</code>를 백업에서도 찾지 못했어요.")
            else:
                out.append("• 이전 버전을 백업에서 열어 확인하지 못했어요.")

    foot = [link(f"{WEB}/{SRC}", "원본"), link(f"{WEB}/{DST}", "백업")]
    if p["before"] and p["after"]:
        foot.append(link(f"{WEB}/{SRC}/compare/{p['before']}...{p['after']}", "변경 내역 보기"))
    out += ["", " · ".join(foot)]
    return "\n".join(out)


def label_for(change):
    name = change["name"]
    if name.startswith("tags/"):
        return ("태그", name[5:])
    if change["src_ref"].startswith("refs/src/pull/"):
        return ("PR 코드", f"#{change['src_ref'].rsplit('/', 1)[-1]}")
    return ("브랜치", change["src_ref"].removeprefix("refs/src/heads/"))


def source_ref(change):
    for a, b in (("refs/src/heads/", "refs/heads/"), ("refs/src/tags/", "refs/tags/")):
        if change["src_ref"].startswith(a):
            return b + change["src_ref"].removeprefix(a)
    return None


# ------------------------------------------------------------- send
def send(text):
    if ENV.get("DRY_RUN") == "1" or not ENV.get("TELEGRAM_BOT_TOKEN") or not ENV.get("TELEGRAM_CHAT_ID"):
        print("----- message -----\n" + text)
        return
    url = f"https://api.telegram.org/bot{ENV['TELEGRAM_BOT_TOKEN']}/sendMessage"
    fields = {"chat_id": ENV["TELEGRAM_CHAT_ID"], "text": text[:4000], "parse_mode": "HTML",
              "link_preview_options": json.dumps({"is_disabled": True})}
    try:
        urllib.request.urlopen(url, urllib.parse.urlencode(fields).encode(), timeout=20)
    except urllib.error.HTTPError as e:
        # Never lose an alert to a formatting problem: resend as plain text.
        print("HTML 전송 실패, 일반 텍스트로 다시 보냅니다:", e.read()[:200])
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
        fields.update(text=plain)
        fields.pop("parse_mode")
        urllib.request.urlopen(url, urllib.parse.urlencode(fields).encode(), timeout=20)
    print("텔레그램 알림을 보냈어요.")


def main():
    changes, pushed, archived = read_transitions(ENV.get("TRANSITIONS_FILE"))
    messages = []
    if changes:
        activity = read_activity()
        load_dst_refs()
        heads = {git("rev-parse", r) for r in lines(git("for-each-ref", "--format=%(refname)", "refs/src/heads"))}
        for change in changes:
            if change["src_ref"].startswith("refs/src/pull/") and change["new"] in heads:
                continue  # same code as a branch push, already reported there
            ref = source_ref(change)
            pushes = chain(ref, change["old"], change["new"], activity) if ref else None
            if pushes:
                pushes = [{"before": None if a["before"] == ZERO else a["before"],
                           "after": None if a["after"] == ZERO else a["after"],
                           "pusher": (a.get("actor") or {}).get("login"), "time": a.get("timestamp"),
                           "type": a.get("activity_type")} for a in pushes]
            else:
                pushes = [{"before": change["old"], "after": change["new"], "pusher": None,
                           "time": None, "type": None}]
            for push in pushes:
                messages.append(message(analyse(push, change, pushed, archived), label_for(change)))
    problems = audit() if GIT_DIR and DST_URL else []
    if problems and (changes or ENV.get("MIRROR_OUTCOME") not in (None, "", "success") or ENV.get("TEST_MESSAGE") == "true"):
        body = ["<b>⚠️ 백업 불일치</b>", "아래 항목은 아직 원본과 같지 않아 백업 완료로 보지 않아요.", ""]
        for label, what, detail in problems[:8]:
            body.append(f"• {E(label)}: {what}")
            body += [f"  {E(d)}" for d in detail[:3]]
        if len(problems) > 8:
            body.append(f"외 {len(problems) - 8}개")
        messages.append("\n".join(body))
    elif not problems and changes:
        print("검사: 원본의 모든 브랜치·태그가 백업과 같아요.", file=sys.stderr)
    issues = ENV.get("ISSUES_SUMMARY_FILE")
    if issues and os.path.exists(issues):
        text = [l.rstrip() for l in open(issues, encoding="utf-8") if l.strip()]
        if text:
            messages.append("<b>📝 원본 이슈·PR 글 변경</b>\n" + E("\n".join(text[:15]))
                            + f"\n\n{link(f'{WEB}/{DST}/issues', '백업 이슈 보기')}")
    failed = [n for n, o in (("코드 백업", ENV.get("MIRROR_OUTCOME")), ("이슈·PR 글 백업", ENV.get("ISSUES_OUTCOME")))
              if o and o != "success"]
    if failed:
        held = [n for n, s in pushed.items() if s == "held"]
        run = ENV.get("RUN_URL")
        messages.append("<b>❌ 백업 실행 실패</b>\n" + E(", ".join(failed)) + " 단계가 실패했어요."
                        + (f"\n이전 버전 보관에 실패해 갱신을 보류한 브랜치: {E(', '.join(h.split('/', 1)[1] for h in held))}" if held else "")
                        + (f"\n{link(run, '실행 기록')}" if run else ""))
    if not messages and ENV.get("TEST_MESSAGE") == "true":
        messages.append("<b>🔔 테스트 알림</b>\n지금은 원본에 새로 바뀐 내용이 없어요.\n\n"
                        + f"{link(f'{WEB}/{SRC}', '원본')} · {link(f'{WEB}/{DST}', '백업')}")
    for m in messages:
        send(m)
    if not messages:
        print("바뀐 내용이 없어 알림을 보내지 않습니다.")


if __name__ == "__main__":
    main()
