from notionchat.ndjson import _join_text_blocks, _repair_split_fence_languages

fence = "`" * 3

def check(name, got, expect):
    if got != expect:
        raise AssertionError(f"{name}: got {got!r} expected {expect!r}")
    print("OK", name)

# char-split python / json (the "thon" / "on" bug)
check(
    "join python chars",
    _join_text_blocks([fence, *list("python"), "import boto3", fence]),
    f"{fence}python\nimport boto3\n{fence}",
)
check(
    "join json chars",
    _join_text_blocks([fence, *list("json"), "{", fence]),
    f"{fence}json\n{{\n{fence}",
)
check(
    "join sh chars",
    _join_text_blocks([fence, *list("sh"), "aws --version", fence]),
    f"{fence}sh\naws --version\n{fence}",
)

# sanitizer tails
check(
    "repair py+thon",
    _repair_split_fence_languages(f"{fence}py\nthon\nimport boto3\n{fence}"),
    f"{fence}python\nimport boto3\n{fence}",
)
check(
    "repair js+on",
    _repair_split_fence_languages(f"{fence}js\non\n{{\n{fence}"),
    f"{fence}json\n{{\n{fence}",
)
check(
    "repair char python",
    _repair_split_fence_languages(f"{fence}\n" + "\n".join("python") + f"\nimport\n{fence}"),
    f"{fence}python\nimport\n{fence}",
)
print("ALL OK")
