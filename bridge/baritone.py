"""Baritone command builder — pure string formatting, no Minescript dependency."""


def goto(x: float, y: float, z: float) -> str:
    return f"#goto {int(x)} {int(y)} {int(z)}"


def mine(block: str, count: int = 0) -> str:
    if count > 0:
        return f"#mine {count} {block}"
    return f"#mine {block}"


def follow(player: str) -> str:
    return f"#follow player {player}"


def explore() -> str:
    return "#explore"


def stop() -> str:
    return "#stop"
