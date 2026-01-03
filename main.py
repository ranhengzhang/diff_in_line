import os
import subprocess
import re
import sys  # 添加 sys 用于读取命令行参数
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Header, Footer, Static, Input, Label
from textual.screen import Screen
from textual.binding import Binding
from textual.reactive import reactive
from rich.text import Text
from rich.style import Style

# ==========================================
# 核心逻辑 (保持不变)
# ==========================================

def find_git_root(start_path):
    """向上寻找 .git 目录"""
    current_path = os.path.abspath(start_path)
    while current_path != os.path.dirname(current_path):
        if os.path.isdir(os.path.join(current_path, '.git')):
            return current_path
        current_path = os.path.dirname(current_path)
    return None

def get_git_diff(repo_path, file_rel_path):
    """执行 git diff 获取数据"""
    cmd = [
        'git', '-C', repo_path, 'diff',
        '--word-diff=plain',
        r'--word-diff-regex=(\W\w+)*\W',
        '--no-color',
        '--unified=0',
        file_rel_path
    ]
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return result.decode('utf-8', errors='replace')
    except subprocess.CalledProcessError as e:
        return f"Error running git: {e.output.decode()}"
    except Exception as e:
        return str(e)

def optimize_tokens(raw_tokens):
    """优化 Token，保留原有的移动检测逻辑"""
    split_tokens = []
    i = 0
    while i < len(raw_tokens):
        curr_type, curr_text = raw_tokens[i]
        if (curr_type == 1 or curr_type == 2) and i + 1 < len(raw_tokens):
            next_type, next_text = raw_tokens[i + 1]
            if (next_type == 1 or next_type == 2) and curr_type != next_type:
                prefix = ""
                suffix = ""
                while len(curr_text) and len(next_text) and curr_text[0] == next_text[0]:
                    prefix += curr_text[0]
                    curr_text = curr_text[1:]
                    next_text = next_text[1:]
                while len(curr_text) and len(next_text) and curr_text[-1] == next_text[-1]:
                    suffix = curr_text[-1] + suffix
                    curr_text = curr_text[:-1]
                    next_text = next_text[:-1]

                if len(prefix) or len(suffix):
                    if len(prefix): split_tokens.append((0, prefix))
                    if len(curr_text): split_tokens.append((curr_type, curr_text))
                    if len(next_text): split_tokens.append((next_type, next_text))
                    if len(suffix): split_tokens.append((0, suffix))
                    i += 2
                    continue
        split_tokens.append((curr_type, curr_text))
        i += 1

    i = 0
    while i < len(split_tokens) - 1:
        if split_tokens[i][0] == split_tokens[i + 1][0]:
            split_tokens[i] = (split_tokens[i][0], split_tokens[i][1] + split_tokens[i + 1][1])
            split_tokens.pop(i + 1)
        else:
            i += 1

    temp_tokens = list(split_tokens)
    processed_indices = set()
    for i in range(len(temp_tokens)):
        if i in processed_indices: continue
        curr_type, curr_text = temp_tokens[i]
        if curr_type == 1 or curr_type == 2:
            target_type = 2 if curr_type == 1 else 1
            match_index = -1
            search_limit = 60
            for j in range(i + 1, min(len(temp_tokens), i + search_limit)):
                if j in processed_indices: continue
                scan_type, scan_text = temp_tokens[j]
                if scan_type == target_type and scan_text == curr_text:
                    match_index = j
                    break

            if match_index != -1:
                processed_indices.add(i)
                processed_indices.add(match_index)
                move_type = 32 if curr_type == 1 else 31
                temp_tokens[i] = (move_type, curr_text)
                temp_tokens[match_index] = (move_type, curr_text)
                for k in range(i + 1, match_index):
                    mid_type, mid_text = temp_tokens[k]
                    temp_tokens[k] = (33, mid_text)
                    processed_indices.add(k)
    return temp_tokens

def parse_diff_to_tokens(raw_text):
    """解析 Git diff 输出为 Token 列表"""
    header_patterns = [r'^diff --git .*(\n|$)', r'^index .*(\n|$)', r'^--- .*(\n|$)', r'^\+\+\+ .*(\n|$)', r'^@@ .*? @@(\n|$)']
    for pat in header_patterns:
        raw_text = re.sub(pat, '', raw_text, flags=re.MULTILINE)
    if raw_text.startswith('\n'): raw_text = raw_text.lstrip('\n')

    pattern = re.compile(r'(\[-[\s\S]*?-\]|\{\+[\s\S]*?\+\})')
    raw_split = pattern.split(raw_text)
    parsed_tokens = []
    for token in raw_split:
        if not token: continue
        if token.startswith('[-') and token.endswith('-]'):
            parsed_tokens.append((1, token[2:-2]))
        elif token.startswith('{+') and token.endswith('+}'):
            parsed_tokens.append((2, token[2:-2]))
        else:
            parsed_tokens.append((0, token))

    optimized = optimize_tokens(parsed_tokens)

    final_data = []
    next_change_id = 1
    current_active_id = 0
    last_was_change = False
    navigable_ids = []

    for t_type, t_text in optimized:
        is_change = (t_type != 0)
        token_id = 0

        if is_change:
            if last_was_change:
                token_id = current_active_id
            else:
                token_id = next_change_id
                current_active_id = next_change_id
                next_change_id += 1
                navigable_ids.append(token_id)
            last_was_change = True
        else:
            token_id = 0
            last_was_change = False

        final_data.append((t_type, t_text, token_id))

    return final_data, navigable_ids

# ==========================================
# UI 组件 (Textual)
# ==========================================

class DiffViewer(Static):
    """自定义 Diff 显示组件"""
    DEFAULT_CSS = """
    DiffViewer {
        width: 100%;
        height: auto;
        padding: 1;
    }
    """

    tokens = reactive([])
    active_change_id = reactive(None)

    def watch_tokens(self, tokens):
        self.render_content()

    def watch_active_change_id(self, old_id, new_id):
        self.render_content()

    def render_content(self):
        if not self.tokens:
            self.update("No content.")
            return

        rich_text = Text()

        # 样式定义
        style_norm = Style()

        # === 非聚焦状态 (Unfocused) ===
        # 当光标不在这个变动上时显示的颜色
        style_del_un = Style(color="#f85149")  # 删除的内容（文字红）
        style_add_un = Style(color="#3fb950")  # 新增的内容（文字绿）
        style_move_un = Style(color="#58a6ff")  # 移动的内容（文字蓝）

        # === 聚焦状态 (Focused) ===
        # 当你按左右键选中某个变动时显示的颜色
        style_del_fo = Style(color="#ffffff", bgcolor="#67060c", bold=True)  # 删除（白字红底）
        style_add_fo = Style(color="#ffffff", bgcolor="#0e4429", bold=True)  # 新增（白字绿底）
        style_move_fo = Style(color="#ffffff", bgcolor="#033d8b", bold=True)  # 移动内容（白字蓝底）

        # === 聚焦状态 (Focused) ===
        # 当你按左右键选中某个变动时显示的颜色
        style_del_pair_fo = Style(color="#cccccc", bgcolor="#67060c", bold=True)  # 删除（白字红底）
        style_add_pair_fo = Style(color="#cccccc", bgcolor="#0e4429", bold=True)  # 新增（白字绿底）
        style_move_pair_fo = Style(color="#cccccc", bgcolor="#033d8b", bold=True)  # 移动内容（白字蓝底）

        for t_type, t_text, t_id in self.tokens:
            is_focused = (t_id == self.active_change_id and t_id != 0)

            if t_type == 0: # Normal
                rich_text.append(t_text, style_norm)
            elif t_type == 33: # Context
                rich_text.append(t_text, style_norm)
            else:
                if is_focused:
                    prefix = ""
                    suffix = ""
                    main_style = style_norm
                    mark_style = style_norm

                    if t_type == 1: # Delete
                        prefix, suffix = "[-", "-]"
                        main_style = style_del_fo
                        mark_style = style_del_pair_fo
                    elif t_type == 2: # Add
                        prefix, suffix = "{+", "+}"
                        main_style = style_add_fo
                        mark_style = style_add_pair_fo
                    elif t_type == 31: # Move Back
                        prefix, suffix = "(<", "<)"
                        main_style = style_move_fo
                        mark_style = style_move_pair_fo
                    elif t_type == 32: # Move Fwd
                        prefix, suffix = "(>", ">)"
                        main_style = style_move_fo
                        mark_style = style_move_pair_fo

                    rich_text.append(prefix, mark_style)
                    rich_text.append(t_text, main_style)
                    rich_text.append(suffix, mark_style)
                else:
                    disp_text = t_text
                    curr_style = style_norm

                    if t_type == 1:
                        disp_text = f"[-{t_text}-]"
                        curr_style = style_del_un
                    elif t_type == 2:
                        disp_text = f"{{+{t_text}+}}"
                        curr_style = style_add_un
                    elif t_type == 31:
                        disp_text = f"(<{t_text}<)"
                        curr_style = style_move_un
                    elif t_type == 32:
                        disp_text = f"(>{t_text}>)"
                        curr_style = style_move_un

                    rich_text.append(disp_text, curr_style)

        self.update(rich_text)


class PathInputScreen(Screen):
    """输入文件路径的屏幕"""

    # 【修复 1】在这里定义 id=error_msg 的样式，而不是传参
    CSS = """
    PathInputScreen {
        align: center middle;
        background: $surface;
    }
    Vertical {
        width: 60%;
        height: auto;
        border: heavy $accent;
        padding: 2;
        background: $panel;
    }
    Label {
        margin-bottom: 1;
    }
    Input {
        width: 100%;
    }
    #error_msg {
        color: red;
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("请输入文件路径 (Git Repository):")
            yield Input(placeholder="/path/to/file", id="path_input")
            # 【修复 1】移除 style 参数，只保留 id
            yield Label("", id="error_msg")

    def on_input_submitted(self, message: Input.Submitted):
        path = message.value.strip().strip("'\"")
        if not path: return
        self.validate_and_submit(path)

    def validate_and_submit(self, path):
        if not os.path.exists(path):
            self.query_one("#error_msg").update(f"错误: 文件不存在")
            return

        abs_path = os.path.abspath(path)
        git_root = find_git_root(os.path.dirname(abs_path))

        if not git_root:
            self.query_one("#error_msg").update(f"错误: 不在 Git 仓库中")
            return

        self.dismiss((git_root, abs_path))


class DiffScreen(Screen):
    """Diff 显示主屏幕"""

    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "new_file", "New File"),
        Binding("left", "prev_change", "Prev Change"),
        Binding("right", "next_change", "Next Change"),
        Binding("h", "prev_change", "Prev Change"),
        Binding("l", "next_change", "Next Change"),
    ]

    def __init__(self, git_root, file_path):
        super().__init__()
        self.git_root = git_root
        self.file_path = file_path
        self.rel_path = os.path.relpath(file_path, git_root)
        self.change_ids = []
        self.current_idx = 0
        self.tokens = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            DiffViewer(id="diff_content"),
            id="scroll_container"
        )
        yield Footer()

    def on_mount(self):
        self.load_diff()

    def load_diff(self):
        raw_diff = get_git_diff(self.git_root, self.rel_path)
        if not raw_diff.strip():
            self.query_one("#diff_content").update(f"\n  No changes found for: {self.rel_path}")
            return

        self.tokens, self.change_ids = parse_diff_to_tokens(raw_diff)

        viewer = self.query_one("#diff_content")
        viewer.tokens = self.tokens

        self.current_idx = 0
        self.update_focus()

    def update_focus(self):
        if not self.change_ids:
            return

        target_id = self.change_ids[self.current_idx]
        viewer = self.query_one(DiffViewer)
        viewer.active_change_id = target_id

        self.title = f"{self.rel_path} [{self.current_idx + 1}/{len(self.change_ids)}]"

        # 简单的滚动估算
        line_count = 0
        target_line = 0
        found = False

        for t_type, t_text, t_id in self.tokens:
            if t_id == target_id and not found:
                target_line = line_count
                found = True
            line_count += t_text.count('\n')

        scroll_container = self.query_one("#scroll_container")
        scroll_container.scroll_to(y=target_line, animate=False)

    def action_next_change(self):
        if self.change_ids and self.current_idx < len(self.change_ids) - 1:
            self.current_idx += 1
            self.update_focus()
        else:
            self.app.bell()

    def action_prev_change(self):
        if self.change_ids and self.current_idx > 0:
            self.current_idx -= 1
            self.update_focus()
        else:
            self.app.bell()

    def action_quit_app(self):
        self.app.exit()

    def action_new_file(self):
        self.app.push_screen(PathInputScreen(), self.app.on_file_selected)


class GitDiffApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #scroll_container {
        height: 1fr;
        overflow-y: scroll;
        border: solid $secondary;
    }
    """

    def on_mount(self):
        # 【修复 2】支持命令行参数直接打开文件
        if len(sys.argv) > 1:
            target = sys.argv[1]
            if os.path.exists(target):
                abs_path = os.path.abspath(target)
                git_root = find_git_root(os.path.dirname(abs_path))
                if git_root:
                    self.push_screen(DiffScreen(git_root, abs_path))
                    return

        # 否则显示输入框
        self.push_screen(PathInputScreen(), self.on_file_selected)

    def on_file_selected(self, result):
        if result:
            git_root, abs_path = result
            self.push_screen(DiffScreen(git_root, abs_path))
        else:
            self.exit()

if __name__ == "__main__":
    app = GitDiffApp()
    app.run()
