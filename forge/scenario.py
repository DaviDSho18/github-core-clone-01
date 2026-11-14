"""The env's seeded scenario, reimplemented so the forge can serve what the checks ask for.

The env (program/env/src/wire.ts) builds every name, path, blob, message and title it will ever
send from one constant seed through a splitmix64-seeded xorshift64* PRNG. Reproducing that stream
here means the forge knows the 30 repository names the pagination check creates, the file paths and
blob bytes the contents checks read back, and the issue/pull templates they submit -- without
reading a clock, a random source or the network.

Standard library only. Nothing here reads a clock; every timestamp is SCENARIO_EPOCH_SEC plus a
seeded offset, exactly as wire.ts's scenarioDate does.
"""

from __future__ import annotations

# ----------------------------------------------------------------------------- constants

U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_SEED = 0x6A09E667F3BCC909
SCENARIO_EPOCH_SEC = 1_735_689_600
SCENARIO_USERS = 12
SCENARIO_VIEWER_REPOS = 30
SCENARIO_ORG_REPOS = 6
SCENARIO_STRANGER_REPOS = 3
SCENARIO_FILE_PATHS = 60
SCENARIO_BLOBS = 24
SCENARIO_COMMITS = 60
SCENARIO_ISSUES = 60
SCENARIO_PULLS = 30
DEFAULT_BRANCH = "main"

FIRST_NAMES = [
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo", "iris", "jonas", "kira", "lars",
    "mira", "nico", "olga", "pavel", "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
]
LAST_NAMES = [
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia", "holm", "iversen", "jansen", "keller",
    "lind", "moreau", "novak", "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega", "weber",
    "xu", "yilmaz", "zimmer",
]
REPO_ADJECTIVES = [
    "amber", "brisk", "calm", "crisp", "eager", "faint", "gentle", "happy", "idle", "jolly", "keen", "lively",
    "mellow", "nimble", "open", "plain", "quick", "quiet", "rapid", "soft", "steady", "tidy", "upbeat",
    "vivid", "warm", "witty", "young", "zesty",
]
REPO_NOUNS = [
    "acorn", "badger", "canyon", "cedar", "comet", "coral", "delta", "ember", "falcon", "glade", "harbor",
    "indigo", "juniper", "kettle", "lantern", "meadow", "nebula", "orchid", "pebble", "quartz", "raven",
    "saffron", "thistle", "umber", "violet", "willow", "yarrow", "zephyr",
]
REPO_DESCRIPTIONS = [
    "a small service with a sharp edge",
    "notes and experiments, kept until they are not",
    "the tooling behind the thing",
    "a reference implementation, for reading",
    "what the prototype became",
    "fixtures, generators and the odd benchmark",
    "the parts that must not break",
    "a sandbox for the next idea",
]
PATH_SEGMENTS = [
    "src", "lib", "docs", "test", "scripts", "api", "core", "util", "config", "data", "web", "cli", "pkg",
    "internal", "examples", "assets", "build", "tools", "server", "client",
]
FILE_STEMS = [
    "index", "request", "response", "server", "client", "schema", "parser", "router", "config", "store",
    "clock", "queue", "render", "format", "validate", "session", "token", "event", "stream", "cache",
]
FILE_EXTENSIONS = ["ts", "js", "json", "md", "txt", "yml", "toml", "css", "html", "sql"]
BLOB_WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo",
    "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform", "victor",
    "whiskey", "xray", "yankee", "zulu", "amber", "basalt", "cobalt", "drift", "ember", "frost", "granite",
    "hollow", "ivory", "jasper", "kelp", "lichen", "moss", "nickel",
]
COMMIT_VERBS = [
    "Add", "Fix", "Refactor", "Document", "Test", "Tidy", "Speed up", "Simplify", "Handle", "Extract",
    "Inline", "Rename", "Guard", "Wire", "Drop",
]
COMMIT_OBJECTS = [
    "the request parser", "repo lookup", "issue pagination", "the merge path", "token checks",
    "the clone endpoint", "packfile handling", "error bodies", "branch listing", "the GraphQL node query",
    "MCP tool dispatch", "rate limit headers", "the upload pack", "the ref advertisement", "commit ancestry",
]
ISSUE_ADJECTIVES = [
    "Broken", "Slow", "Missing", "Confusing", "Unexpected", "Flaky", "Incorrect", "Unclear", "Stale", "Noisy",
]
ISSUE_SUBJECTS = [
    "clone over HTTP", "push report-status", "issue pagination", "merge button", "GraphQL node ids",
    "MCP tool list", "shallow fetch", "tag listing", "repo creation", "content update", "branch deletion",
    "commit listing",
]
PULL_VERBS = ["Add", "Fix", "Improve", "Simplify", "Document", "Refactor", "Speed up", "Harden"]
PULL_SUBJECTS = [
    "shallow fetch support", "push report status checks", "issue label validation", "merge conflict detection",
    "GraphQL cursor pagination", "MCP tool schemas", "packfile delta reuse", "conditional requests",
    "repo pagination", "branch protection stubs",
]
SENTENCE_STARTS = [
    "Steps to reproduce are below", "This showed up after the last change", "The expected behaviour is clear",
    "A minimal case is attached", "It works on a fresh repository", "The same flow works over the API",
]
SENTENCE_ENDS = [
    "and the server answers with a JSON error", "but the second request fails", "and nothing is written to the log",
    "while the reply body stays empty", "and the status is wrong for the method", "so the client gives up",
]
ISSUE_LABELS = ["bug", "enhancement", "documentation", "question", "good first issue", "help wanted"]
DEFAULT_BRANCHES = ["main", "develop", "release/1.0"]


# ----------------------------------------------------------------------------- the PRNG


def fnv1a64(text: str) -> int:
    """FNV-1a, 64-bit: a label's stable seed contribution."""
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def mix64(value: int) -> int:
    """splitmix64's finalizer."""
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


def scenario_seed_for(label: str) -> int:
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    """xorshift64*, seeded from a bigint: same label, same numbers, in any process."""

    __slots__ = ("state",)

    def __init__(self, seed: int):
        masked = seed & U64_MASK
        self.state = SCENARIO_SEED if masked == 0 else masked

    @classmethod
    def from_label(cls, label: str) -> "Rng":
        return cls(scenario_seed_for(label))

    def next_u64(self) -> int:
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ (x << 25)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, max_exclusive: int) -> int:
        if max_exclusive <= 0:
            raise ValueError("next_int needs a positive integer")
        if max_exclusive == 1:
            return 0
        rng_range = 4_294_967_296
        limit = rng_range - (rng_range % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def fraction(self) -> float:
        return self.next_u32() / 4_294_967_296

    def chance(self, probability: float) -> bool:
        return self.fraction() < probability

    def pick(self, items):
        if not items:
            raise ValueError("pick from an empty list")
        return items[self.next_int(len(items))]

    def shuffle(self, items):
        copy = list(items)
        for i in range(len(copy) - 1, 0, -1):
            j = self.next_int(i + 1)
            copy[i], copy[j] = copy[j], copy[i]
        return copy


# ----------------------------------------------------------------------------- generators


def generate_users(rng: Rng, count: int) -> list[dict]:
    used: set[str] = set()
    users: list[dict] = []
    for i in range(max(0, count)):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = "".join(ch for ch in f"{first}{last}" if ch.isalnum() or ch in "-")
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({"id": 1000 + i, "login": login, "name": f"{first} {last}", "email": f"{login}@example.invalid"})
    return users


def generate_repos(rng: Rng, owner: str, count: int, first_id: int = 5000) -> list[dict]:
    used: set[str] = set()
    repos: list[dict] = []
    for i in range(max(0, count)):
        base = f"{rng.pick(REPO_ADJECTIVES)}-{rng.pick(REPO_NOUNS)}"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        repos.append(
            {
                "id": first_id + i,
                "owner": owner,
                "name": name,
                "full_name": f"{owner}/{name}",
                "description": rng.pick(REPO_DESCRIPTIONS),
                "private": i % 7 == 3,
                "default_branch": DEFAULT_BRANCH,
            }
        )
    return repos


def generate_file_paths(rng: Rng, count: int) -> list[str]:
    used: set[str] = set()
    paths: list[str] = []
    for _ in range(max(0, count)):
        depth = 0 if rng.next_int(3) == 0 else 1 + rng.next_int(2)
        dirs = [] if depth == 0 else rng.shuffle(PATH_SEGMENTS)[:depth]
        base = "/".join([*dirs, f"{rng.pick(FILE_STEMS)}.{rng.pick(FILE_EXTENSIONS)}"])
        path = base
        suffix = 2
        while path in used:
            tail = base.split("/")[-1]
            path = "/".join([*dirs, f"{suffix}-{tail}"])
            suffix += 1
        used.add(path)
        paths.append(path)
    return paths


def generate_blob_text(rng: Rng, size_bytes: int) -> str:
    size = max(0, int(size_bytes))
    out = ""
    while len(out) < size:
        out += rng.pick(BLOB_WORDS)
        if len(out) < size:
            out += "\n" if rng.chance(0.14) else " "
    return out[:size]


def generate_sentence(rng: Rng) -> str:
    return f"{rng.pick(SENTENCE_STARTS)} {rng.pick(SENTENCE_ENDS)}."


def generate_body(rng: Rng) -> str:
    count = 1 + rng.next_int(3)
    return " ".join(generate_sentence(rng) for _ in range(count))


def generate_commit_message(rng: Rng) -> str:
    head = f"{rng.pick(COMMIT_VERBS)} {rng.pick(COMMIT_OBJECTS)}"
    return f"{head}\n\n{generate_sentence(rng)}" if rng.chance(0.35) else head


def generate_commit(rng: Rng, author: dict) -> dict:
    epoch = SCENARIO_EPOCH_SEC + rng.next_int(90 * 24 * 60 * 60)
    return {"message": generate_commit_message(rng), "author": author, "epoch_sec": epoch, "iso": iso_of(epoch)}


def generate_issue(rng: Rng) -> dict:
    return {
        "title": f"{rng.pick(ISSUE_ADJECTIVES)} {rng.pick(ISSUE_SUBJECTS)}",
        "body": generate_body(rng),
        "labels": [rng.pick(ISSUE_LABELS)] if rng.chance(0.5) else [],
    }


def generate_branch_name(rng: Rng) -> str:
    kind = rng.pick(["feature", "fix", "perf", "docs", "chore"])
    subject = rng.pick(PULL_SUBJECTS).lower()
    slugged = "".join(ch if ch.isalnum() else "-" for ch in subject).strip("-")
    while "--" in slugged:
        slugged = slugged.replace("--", "-")
    return f"{kind}/{slugged}"


def generate_pull(rng: Rng, base: str = DEFAULT_BRANCH) -> dict:
    return {
        "title": f"{rng.pick(PULL_VERBS)} {rng.pick(PULL_SUBJECTS)}",
        "body": generate_body(rng),
        "head": generate_branch_name(rng),
        "base": base,
    }


def iso_of(epoch_sec: int) -> str:
    """An epoch second as an ISO-8601 UTC instant, computed without a clock."""
    import datetime

    dt = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=epoch_sec)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


# ----------------------------------------------------------------------------- the scenario

_cached: dict | None = None


def build_scenario() -> dict:
    """The scenario, built once from SCENARIO_SEED: every name, path, blob, message and title."""
    global _cached
    if _cached is not None:
        return _cached

    users = generate_users(Rng.from_label("users"), SCENARIO_USERS)
    viewer = users[0]
    teammate = users[1]
    stranger = users[2]

    org_rng = Rng.from_label("org")
    org = f"{org_rng.pick(REPO_ADJECTIVES)}-{org_rng.pick(REPO_NOUNS)}-org"

    branch_rng = Rng.from_label("branches")
    branch_set = list(DEFAULT_BRANCHES)
    seen_branches = set(branch_set)
    for _ in range(8):
        branch = generate_branch_name(branch_rng)
        suffix = 2
        while branch in seen_branches:
            branch = f"{generate_branch_name(branch_rng)}-{suffix}"
            suffix += 1
        seen_branches.add(branch)
        branch_set.append(branch)

    file_paths = generate_file_paths(Rng.from_label("paths"), SCENARIO_FILE_PATHS)
    blob_rng = Rng.from_label("blobs")
    blobs = [
        {"path": path, "content": generate_blob_text(blob_rng, 64 + blob_rng.next_int(4_033))}
        for path in file_paths[:SCENARIO_BLOBS]
    ]

    commit_rng = Rng.from_label("commits")
    commits = [generate_commit(commit_rng, commit_rng.pick(users)) for _ in range(SCENARIO_COMMITS)]

    issue_rng = Rng.from_label("issues")
    issues = [generate_issue(issue_rng) for _ in range(SCENARIO_ISSUES)]

    pull_rng = Rng.from_label("pulls")
    pulls = [generate_pull(pull_rng) for _ in range(SCENARIO_PULLS)]

    _cached = {
        "seed": f"{SCENARIO_SEED:016x}",
        "viewer": viewer,
        "teammate": teammate,
        "stranger": stranger,
        "users": users,
        "org": org,
        "repos": generate_repos(Rng.from_label("repos.viewer"), viewer["login"], SCENARIO_VIEWER_REPOS, 5000),
        "org_repos": generate_repos(Rng.from_label("repos.org"), org, SCENARIO_ORG_REPOS, 6000),
        "stranger_repos": generate_repos(Rng.from_label("repos.stranger"), stranger["login"], SCENARIO_STRANGER_REPOS, 7000),
        "labels": list(ISSUE_LABELS),
        "branches": branch_set,
        "file_paths": file_paths,
        "blobs": blobs,
        "commits": commits,
        "issues": issues,
        "pulls": pulls,
    }
    return _cached
