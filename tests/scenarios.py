"""Play push scenarios against local source/backup repositories and check the
messages scripts/report.py builds. Run: python3 tests/scenarios.py
With SEND=1 (and TELEGRAM_* set) the example messages are really sent, marked
as examples. Nothing here touches GitHub repositories."""
import html.parser
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = tempfile.mkdtemp()
SRC, DST, WORK = f"{T}/src.git", f"{T}/dst.git", f"{T}/work"
clock = [0]


def sh(*args, cwd=WORK, env=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise SystemExit(f"{args}: {r.stderr}")
    return r.stdout.strip()


def commit(who, files, msg):
    for path, text in files.items():
        full = os.path.join(WORK, path)
        if text is None:
            os.remove(full)
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write(text)
    sh("git", "add", "-A")
    clock[0] += 60
    date = f"2026-11-10T0{clock[0] // 3600}:{clock[0] // 60 % 60:02d}:00Z"
    sh("git", "-c", f"user.name={who}", "-c", f"user.email={who}@example.com", "commit", "-qm", msg,
       env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return sh("git", "rev-parse", "HEAD")


def head():
    return sh("git", "rev-parse", "HEAD")


def remote_tip(branch):
    return sh("git", "--git-dir", SRC, "rev-parse", f"refs/heads/{branch}", check=False) or "0" * 40


activity = []


def push(branch, pusher, force=False, kind=None):
    before = remote_tip(branch)
    sh("git", "push", "-q", *(["-f"] if force else []), SRC, f"HEAD:refs/heads/{branch}")
    after = remote_tip(branch)
    activity.insert(0, {"ref": f"refs/heads/{branch}", "before": before, "after": after,
                        "actor": {"login": pusher}, "timestamp": "2026-11-10T05:%02d:00Z" % (len(activity) % 60),
                        "activity_type": kind or ("force_push" if force else "branch_creation" if before == "0" * 40 else "push")})


run_no = [0]


def backup(note):
    run_no[0] += 1
    d = f"{T}/run{run_no[0]}"
    os.makedirs(d)
    env = {"SRC_URL": SRC, "DST_URL": DST, "WORK_DIR": f"{d}/git", "TRANSITIONS_FILE": f"{d}/t.tsv",
           "STAMP": f"20261110T05{run_no[0]:02d}Z"}
    r = subprocess.run(["bash", f"{ROOT}/scripts/mirror.sh"], capture_output=True, text=True, env={**os.environ, **env})
    json.dump(activity, open(f"{d}/activity.json", "w"))
    out = sh(sys.executable, f"{ROOT}/scripts/report.py", cwd=d, env={
        **env, "SRC_REPO": "SYU-Taxi-Delivery/taxi-delivery-sharing", "DST_REPO": "jennjeong/taxi-delivery-sharing",
        "ACTIVITY_FILE": f"{d}/activity.json", "NO_API": "1", "DRY_RUN": "1",
        "MIRROR_OUTCOME": "success" if r.returncode == 0 else "failure"})
    msgs = [m.strip() for m in out.split("----- message -----") if m.strip()]
    last_run["mirror"] = r.stdout + r.stderr
    return msgs


last_run = {}


def latest_matches(skip=()):
    """Every source branch has the same commit and files in the backup."""
    src = dict(l.split("\t")[::-1] for l in sh("git", "ls-remote", SRC, "refs/heads/*", cwd=T).splitlines())
    dst = dict(l.split("\t")[::-1] for l in sh("git", "ls-remote", DST, cwd=T).splitlines())
    for ref, sha in src.items():
        b = ref.removeprefix("refs/heads/")
        if b in skip:
            continue
        mine = dst.get("refs/heads/source/main" if b == "main" else ref)
        assert mine == sha, f"backup {b} = {mine}, source = {sha}"
        a = sh("git", "--git-dir", SRC, "rev-parse", f"{sha}^{{tree}}", cwd=T)
        z = sh("git", "--git-dir", DST, "rev-parse", f"{mine}^{{tree}}", cwd=T)
        assert a == z, b
    return len(src) - len(skip)


def restore(msg, path):
    """Recover `path` using only the backup: the version link in the message
    names a backup ref; clone the backup alone and read the file there."""
    import re
    ref = re.search(r'href="[^"]*/tree/([^"]+)">[^<]*</a> \(커밋', msg).group(1)
    d = tempfile.mkdtemp()
    sh("git", "clone", "-q", "--no-checkout", DST, d, cwd=T)
    sh("git", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*", cwd=d)
    rev = ref if len(ref) == 40 else f"origin/{ref}"
    return sh("git", "show", f"{rev}:{path}", cwd=d)


class Check(html.parser.HTMLParser):
    """Telegram HTML: only known tags, all closed."""
    OK = {"b", "a", "code"}

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.OK, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, tag


def well_formed(m):
    c = Check()
    c.feed(m)
    c.close()
    assert not c.stack, c.stack


results = []


def expect(name, msgs, has=(), lacks=()):
    assert len(msgs) == 1, f"{name}: {len(msgs)} messages\n" + "\n\n".join(msgs)
    m = msgs[0]
    well_formed(m)
    for s in has:
        assert s in m, f"{name}: missing {s!r}\n{m}"
    for s in lacks:
        assert s not in m, f"{name}: unexpected {s!r}\n{m}"
    results.append((name, m))
    print(f"ok  {name}")


# ---------------------------------------------------------------- setup
sh("git", "init", "-q", "--bare", SRC, cwd=T)
sh("git", "init", "-q", "--bare", DST, cwd=T)
sh("git", "init", "-q", "-b", "main", WORK, cwd=T)
commit("alice", {"README.md": "두유팟\n", "src/app.ts": "export const a = 1;\n"}, "프로젝트 시작")
commit("bob", {"src/login.ts": "login v1\n"}, "로그인 화면")
push("main", "alice")
for b in ("s1", "s2", "s3", "s4", "s5"):
    push(b, "alice")
backup("initial")

# 1) Normal push. Also: alice edits bob's line differently (not a loss),
#    and a subject with Telegram-special characters.
sh("git", "checkout", "-q", "-B", "s1", "main")
commit("alice", {"src/login.ts": "login v2 (alice rewrote)\n"}, "로그인 문구 <수정> & 정리")
commit("alice", {"src/app.ts": "export const a = 2;\n"}, "상수 변경")
push("s1", "alice")
expect("1. 일반 푸시", backup("normal"),
       has=["🔔 GitHub 원본 변경 감지", "브랜치: s1", "푸시한 사람: alice", "시각: 2026-11-10",
            "로그인 문구 &lt;수정&gt; &amp; 정리", "💾 최신 상태 반영", "• 완료: 백업", "변경 내역 보기"],
       lacks=["⚠️", "🛟"])
assert latest_matches()

# 2) Force push that drops bob's commits.
sh("git", "checkout", "-q", "-B", "s2", "main")
commit("bob", {"src/pay.ts": "pay\n"}, "결제 모듈 추가")
commit("bob", {"src/app.ts": "export const a = 1; // bob\n"}, "앱 상수 주석")
push("s2", "bob")
backup("bob work")
sh("git", "reset", "-q", "--hard", "main")
commit("alice", {"src/app.ts": "export const a = 3;\n"}, "앱 상수 3")
push("s2", "alice", force=True)
expect("2. 다른 작성자 커밋이 빠진 강제 푸시", backup("force"),
       has=["⚠️ 변경 유실 주의", "(강제 푸시)", "푸시한 사람: alice", "결제 모듈 추가 — 작성자 bob",
            "변경 내용도 현재 브랜치에 없음 (src/pay.ts 없음)", "앱 상수 주석 — 작성자 bob · 현재 브랜치 이력에서 빠짐",
            "🛟 이전 버전 복구 가능", "archive/s2/", "백업에서 열리는 것 확인"])
assert restore(results[-1][1], "src/pay.ts") == "pay"
assert restore(results[-1][1], "src/app.ts") == "export const a = 1; // bob"
assert latest_matches()

# 3) Rebase (and squash) that keeps bob's changes.
sh("git", "checkout", "-q", "-B", "s3", "main")
c1 = commit("bob", {"src/chat.ts": "chat\n"}, "채팅 추가")
c2 = commit("bob", {"src/chat.ts": "chat\nsend\n"}, "채팅 전송")
c3 = commit("bob", {"docs/a.md": "doc\n"}, "문서")
push("s3", "bob")
backup("bob s3")
sh("git", "checkout", "-q", "main")
commit("alice", {"README.md": "두유팟 v2\n"}, "README 갱신")
push("main", "alice")
sh("git", "checkout", "-q", "-B", "s3", "main")
sh("git", "cherry-pick", c1, c2, env={"GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com"})
sh("git", "reset", "-q", "--soft", "HEAD~2")
commit("alice", {}, "채팅 기능 (합침)")
sh("git", "cherry-pick", c3, env={"GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "alice@example.com"})
push("s3", "alice", force=True)
msgs = backup("rebase")
msgs = [m for m in msgs if "브랜치: s3" in m]
expect("3. 내용이 보존된 리베이스", msgs,
       has=["(강제 푸시)", "같은 변경이 현재 브랜치에 남아 있어요", "🛟 이전 버전 복구 가능"],
       lacks=["⚠️", "이력에서 빠짐"])

# 4) Normal push that undoes bob's change: stale copy of a file, and a merge
#    that keeps one side.
sh("git", "checkout", "-q", "-B", "s4", "main")
commit("bob", {"src/app.ts": "export const a = 1;\nexport const b = 2; // bob\n"}, "상수 b 추가")
push("s4", "bob")
backup("bob s4")
commit("alice", {"src/app.ts": "export const a = 1;\n", "src/other.ts": "x\n"}, "다른 기능")
push("s4", "alice")
expect("4a. 일반 푸시에서 기존 변경이 빠짐 (예전 파일로 덮어씀)", backup("stale copy"),
       has=["⚠️ 변경 유실 주의", "덮어쓴 변경", "src/app.ts", "시점 내용으로 되돌아감", "빠진 변경:", "상수 b 추가 — 작성자 bob"],
       lacks=["(강제 푸시)", "🛟"])

sh("git", "checkout", "-q", "-B", "s4m", "main")
commit("bob", {"src/login.ts": "login v1\nremember me\n"}, "로그인 유지 옵션")
bob_tip = head()
sh("git", "checkout", "-q", "-B", "s4", "s4")
commit("alice", {"src/login.ts": "login v1\nsocial login\n"}, "소셜 로그인")
sh("git", "-c", "user.name=alice", "-c", "user.email=alice@example.com", "merge", "-q", "-X", "ours", "--no-edit", bob_tip)
push("s4", "alice")
expect("4b. 병합에서 한쪽 내용만 남김", backup("merge"),
       has=["⚠️ 변경 유실 주의", "병합", "한쪽 내용만 남음", "src/login.ts", "로그인 유지 옵션"])

# 4c) Control: ordinary deletion of bob's line is not called a loss.
commit("alice", {"src/login.ts": "login v1\n"}, "소셜 로그인 줄 정리")
push("s4", "alice")
expect("4c. 일반 줄 삭제 (주의 아님)", backup("plain edit"), lacks=["⚠️"])

# 5) Overwrite whose previous state is gone: bob pushed X and alice
#    force-pushed over it before the next backup; X no longer exists.
sh("git", "checkout", "-q", "-B", "s5", "main")
commit("bob", {"src/x.ts": "x\n"}, "임시 작업")
x = head()
sh("git", "push", "-q", f"{T}/scratch-x.git", "HEAD", check=False)
activity.insert(0, {"ref": "refs/heads/s5", "before": remote_tip("s5"), "after": x,
                    "actor": {"login": "bob"}, "timestamp": "2026-11-10T05:50:00Z", "activity_type": "push"})
sh("git", "reset", "-q", "--hard", "main")
commit("alice", {"src/y.ts": "y\n"}, "새 작업")
before = remote_tip("s5")
sh("git", "push", "-q", "-f", SRC, "HEAD:refs/heads/s5")
activity.insert(0, {"ref": "refs/heads/s5", "before": x, "after": head(),
                    "actor": {"login": "alice"}, "timestamp": "2026-11-10T05:51:00Z", "activity_type": "force_push"})
msgs = backup("unreadable")
expect("5. 이전 버전을 비교할 수 없음", [m for m in msgs if "푸시한 사람: alice" in m],
       has=["⚠️ 덮어쓰기 감지 · 유실 여부 확인 필요", "확인 범위", "읽지 못해", "변경 내역 보기",
            "⚠️ 이전 버전 복구 미확인", "백업에서도 찾지 못했어요", "새 작업 — 작성자 alice"],
       lacks=["⚠️ 변경 유실 주의", "🛟"])
bob_msg = [m for m in msgs if "푸시한 사람: bob" in m][0]
assert "반영 못함" in bob_msg, bob_msg
results.append(("5. (같은 시간대 bob 푸시)", bob_msg))

# 6) Without push details (activity API unavailable): one message per ref.
sh("git", "checkout", "-q", "s1")
commit("alice", {"src/app.ts": "export const a = 9;\n"}, "상수 9")
push("s1", "alice")
activity.clear()
expect("6. 푸시 정보 없이", backup("no activity"),
       has=["🔔 GitHub 원본 변경 감지", "상수 9", "• 완료: 백업"], lacks=["푸시한 사람", "시각:"])

# 7) Normal push that deletes a teammate's file: latest follows the source,
#    and the previous version is kept and restorable.
sh("git", "checkout", "-q", "-B", "s7", "main")
commit("bob", {"docs/guide.md": "# 가이드\n팀 규칙 정리\n"}, "가이드 문서 추가")
push("s7", "bob")
backup("bob s7")
commit("alice", {"docs/guide.md": None, "README.md": "두유팟 v3\n"}, "문서 정리")
push("s7", "alice")
expect("7. 일반 푸시에서 팀원 파일 삭제", backup("delete file"),
       has=["삭제된 파일", "• docs/guide.md", "• 완료: 백업", "🛟 이전 버전 복구 가능", "archive/s7/", "• 파일:",
            "docs/guide.md</a>"], lacks=["⚠️"])
assert restore(results[-1][1], "docs/guide.md") == "# 가이드\n팀 규칙 정리"
assert latest_matches()

# 8) Branch deleted in the source with a teammate's unmerged commit.
sh("git", "checkout", "-q", "-B", "s8", "main")
commit("bob", {"src/report.ts": "report\n"}, "리포트 화면")
push("s8", "bob")
backup("bob s8")
before = remote_tip("s8")
sh("git", "push", "-q", SRC, ":refs/heads/s8")
activity.insert(0, {"ref": "refs/heads/s8", "before": before, "after": "0" * 40, "actor": {"login": "alice"},
                    "timestamp": "2026-11-10T05:58:00Z", "activity_type": "branch_deletion"})
expect("8. 브랜치 삭제", backup("delete branch"),
       has=["⚠️ 변경 유실 주의", "(브랜치 삭제)", "푸시한 사람: alice", "리포트 화면 — 작성자 bob · 원본 어느 브랜치에도 없음",
            "백업의 같은 이름 브랜치는 지우지 않고", "🛟 이전 버전 복구 가능", "-deleted"])
assert restore(results[-1][1], "src/report.ts") == "report"
assert sh("git", "--git-dir", DST, "rev-parse", "refs/heads/s8", cwd=T) == before, "backup branch kept"
again = backup("again"); assert "no changes" in last_run["mirror"] and not any("🔔" in m for m in again), again

# 9) Keeping the previous version fails: the backup branch is held as it was.
hook = f"{DST}/hooks/pre-receive"
open(hook, "w").write("#!/bin/sh\nwhile read o n r; do case $r in refs/heads/archive/*) echo no archive >&2; exit 1;; esac; done\n")
os.chmod(hook, 0o755)
sh("git", "checkout", "-q", "s2")
held_before = remote_tip("s2")
sh("git", "reset", "-q", "--hard", "main")
commit("alice", {"src/app.ts": "export const a = 4;\n"}, "앱 상수 4")
push("s2", "alice", force=True)
msgs = backup("hold")
assert sh("git", "--git-dir", DST, "rev-parse", "refs/heads/s2", cwd=T) == held_before, "backup s2 untouched"
assert any("❌ 백업 실행 실패" in m and "보류한 브랜치: s2" in m for m in msgs), msgs
expect("9. 이전 버전 보관 실패 → 갱신 보류", [m for m in msgs if "브랜치: s2" in m and "🔔" in m],
       has=["• 보류: 이전 버전을 보관·검증하지 못해 기존 백업을 그대로 뒀어요", "🛟 이전 버전 복구 가능"])
results.append(("9. (실패 알림)", [m for m in msgs if "❌" in m][0]))
os.remove(hook)
msgs = backup("retry")
assert any("브랜치: s2" in m and "• 완료: 백업" in m and "archive/s2/" in m for m in msgs), msgs
assert latest_matches()
print("ok  9b. 다음 실행에서 보관 후 반영")

print(f"\n{len(results)} scenarios passed")
if os.environ.get("SHOW") == "1" or os.environ.get("SEND") == "1":
    sys.path.insert(0, f"{ROOT}/scripts")
    os.environ.pop("DRY_RUN", None)
    import report
    for name, m in results:
        text = f"<b>[예시] {report.E(name)}</b>\n\n{m}"
        if os.environ.get("SEND") == "1":
            report.send(text)
        else:
            print("\n=====", name, "=====\n" + m)
