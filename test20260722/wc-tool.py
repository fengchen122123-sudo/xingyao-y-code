import argparse
import sys


def count_lines(content: str) -> int:
    # 使用 splitlines() 并处理尾随无换行的情况
    lines = content.splitlines()
    if content and not content.endswith("\n"):
        # 文件非空且最后没有换行符，最后一行仍应计数
        return len(lines)
    return len(lines)


def count_words(content: str) -> int:
    return len(content.split())


def count_chars(content: str) -> int:
    return len(content)


def main():
    parser = argparse.ArgumentParser(description="Count lines, words, and characters in a file.")
    parser.add_argument("filename", help="Path to the input file")
    parser.add_argument("-l", action="store_true", help="Count lines")
    parser.add_argument("-w", action="store_true", help="Count words")
    parser.add_argument("-c", action="store_true", help="Count characters")

    args = parser.parse_args()

    try:
        with open(args.filename, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: File '{args.filename}' not found.", file=sys.stderr)
        sys.exit(1)

    lines = count_lines(content)
    words = count_words(content)
    chars = count_chars(content)

    # 无参数时默认显示全部三项
    show_lines = args.l or (not args.l and not args.w and not args.c)
    show_words = args.w or (not args.l and not args.w and not args.c)
    show_chars = args.c or (not args.l and not args.w and not args.c)

    parts = []
    if show_lines:
        parts.append(str(lines))
    if show_words:
        parts.append(str(words))
    if show_chars:
        parts.append(str(chars))

    print("\t".join(parts))


if __name__ == "__main__":
    main()
