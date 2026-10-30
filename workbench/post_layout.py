"""Plain-text mobile typography. Whitespace-only normalization, no rewriting."""
import re


LAYOUT_RULES = '''排版要求：适合手机阅读。标题独占一段，标题与正文、每个编号条目、每个新闻段落之间空一行。
标题尽量简短，通常不超过35字；教程一项一段、每项1～2句话；新闻一段2～3句，建议每段60～120字，任何单段不超过180字。
不要在一句话中为了凑行宽手动折行；需要简短操作或参考指令时，标签独立成段，内容保持简洁。
末尾3～5个相关话题标签放同一行，用一个空格分隔，前面空一行。不使用装饰性分隔线、密集表情、Markdown加粗、代码围栏或表格。
不重复正文标题，不重复标签。不要为了排版改动功能名、版本号、引语和事实。'''


def format_post(body):
    lines = [line.strip() for line in body.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    tag_lines = []
    while lines and re.fullmatch(r'(?:#[^\s#]+\s*)+', lines[-1]):
        tag_lines.insert(0, lines.pop())
    if tag_lines:
        lines.append(' '.join(re.findall(r'#[^\s#]+', ' '.join(tag_lines))))
    return '\n\n'.join(lines)


def layout_issues(body):
    issues = []
    lines = body.strip().splitlines()
    if lines and len(lines[0]) > 52:
        issues.append('标题过长，请精简到52字以内')
    if any(a.strip() and b.strip() for a,b in zip(lines, lines[1:])):
        issues.append('标题、条目、段落和标签之间需要空一行')
    if any(len(block.strip()) > 180 for block in re.split(r'\n\s*\n', body.strip())):
        issues.append('单段超过180字，请缩短或按语义分段')
    if re.search(r'\*\*|```|(?<!`)`[^`\n]+`(?!`)|(?m:^\s*(?:[>|]|#{1,6}\s|[-*_]{3,}\s*$))', body):
        issues.append('纯文字帖子不使用Markdown加粗、代码、表格或装饰分隔线')
    tags = re.findall(r'#[^\s#]+', lines[-1] if lines else '')
    if len(tags) != len(set(tags)):
        issues.append('话题标签不应重复')
    return issues
