"""Protocol instructions outside Markdown examples; shared with Go's reader."""


def has_review_again(body: str) -> bool:
    """Match the established exact line with Markdown fences excluded.

    Openers have at most three leading spaces and at least three backticks or
    tildes. A close uses the same character, at least the opener's length, and
    only trailing whitespace. An unclosed fence quotes the remaining comment.
    """
    fence = None
    fence_length = 0
    for line in body.split('\n'):
        if fence is None and line == 'pp:review-again':
            return True
        text = line.lstrip(' ')
        if len(line)-len(text) > 3 or len(text) < 3 or text[0] not in '`~':
            continue
        length = 1
        while length < len(text) and text[length] == text[0]:
            length += 1
        if fence is not None:
            if text[0] == fence and length >= fence_length and not text[length:].strip(' \t\r'):
                fence, fence_length = None, 0
            continue
        if length >= 3 and (text[0] != '`' or '`' not in text[length:]):
            fence, fence_length = text[0], length
    return False
