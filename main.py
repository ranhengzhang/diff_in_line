import os
import subprocess
import re
import sys
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Header, Footer, Static, Input, Label
from textual.screen import Screen
from textual.binding import Binding
from textual.reactive import reactive
from rich.text import Text
from rich.style import Style
from rich.cells import cell_len  # 引入 Rich 的字符宽度计算工具

# ==========================================
# 核心逻辑 (保持不变)
# ==========================================

def find_git_root(start_path):
    current_path = os.path.abspath(start_path)
    while current_path != os.path.dirname(current_path):
        if os.path.isdir(os.path.join(current_path, '.git')):
            return current_path
        current_path = os.path.dirname(current_path)
    return None

def get_git_diff(repo_path, file_rel_path):
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
    """自定义 Diff 显示组件，使用手动计算的硬折行"""
    DEFAULT_CSS = """
    DiffViewer {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    tokens = reactive([])
    active_change_id = reactive(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.line_y_map = {} # 缓存: Change ID -> 视觉行号

    def watch_tokens(self, tokens):
        """当数据改变时，重绘"""
        self._reflow_text()

    def watch_active_change_id(self, old_id, new_id):
        """当焦点改变时，重绘"""
        self._reflow_text()

    def on_resize(self, event):
        """核心：当尺寸改变时，手动重新计算每一行的断点"""
        self._reflow_text(width=event.size.width)

    def _get_available_width(self):
        """获取用于文本显示的有效宽度"""
        width = self.size.width
        if width == 0:
            width = self.app.size.width
        # 减去 padding (CSS中定义 padding: 0 1，即左右各1)
        return max(1, width - 2)

    def _split_text_by_cell_width(self, text, max_len):
        """
        手动截断字符串，使其视觉宽度不超过 max_len。
        这是手动实现 textwrap 逻辑的关键，支持中文字符。
        返回: (first_part, remainder, visual_width_of_first)
        """
        if not text: return "", "", 0

        # 快速检查：如果总长度小于限制，直接返回
        total_w = cell_len(text)
        if total_w <= max_len:
            return text, "", total_w

        # 慢速逐字检查
        current_w = 0
        for i, char in enumerate(text):
            char_w = cell_len(char)
            if current_w + char_w > max_len:
                return text[:i], text[i:], current_w
            current_w += char_w

        return text, "", total_w

    def _reflow_text(self, width=None):
        """
        手动流式布局引擎。
        1. 遍历所有 Token
        2. 手动拼接每一行
        3. 遇到边界强制换行
        4. 同时记录 ID 对应的行号
        """
        if not self.tokens:
            self.update("No content.")
            return

        if width is None:
            width = self._get_available_width()
        else:
            width = max(1, width - 2)

        final_rich_text = Text()
        self.line_y_map = {} # 重置映射

        # 样式定义
        style_norm = Style(color="#cccccc")
        style_del_un = Style(color="#f85149")
        style_add_un = Style(color="#3fb950")
        style_move_un = Style(color="#58a6ff")

        style_del_fo = Style(color="white", bgcolor="#a01818", bold=True)
        style_add_fo = Style(color="white", bgcolor="#0e4429", bold=True)
        style_move_fo = Style(color="white", bgcolor="#033d8b", bold=True)

        # 布局状态变量
        current_line_visual_width = 0
        current_line_index = 0

        # 辅助函数：向当前布局追加文本片段
        def append_span(text_str, style_obj, change_id):
            nonlocal current_line_visual_width, current_line_index

            # 如果 change_id 有效，记录它所在的行号
            if change_id and change_id != 0 and change_id not in self.line_y_map:
                self.line_y_map[change_id] = current_line_index

            remaining_text = text_str
            while remaining_text:
                # 计算当前行还能放多少宽度的字
                space_left = width - current_line_visual_width

                # 如果当前行已满，强制换行
                if space_left <= 0:
                    final_rich_text.append("\n")
                    current_line_index += 1
                    current_line_visual_width = 0
                    space_left = width

                # 截取适合当前行剩余空间的文本
                # 这里实现了"任意位置换行"，不考虑单词边界
                chunk, left_over, chunk_width = self._split_text_by_cell_width(remaining_text, space_left)

                # 如果连一个字都放不下（chunk为空但还有剩余），强制换行再试
                if not chunk and remaining_text:
                    final_rich_text.append("\n")
                    current_line_index += 1
                    current_line_visual_width = 0
                    space_left = width
                    chunk, left_over, chunk_width = self._split_text_by_cell_width(remaining_text, space_left)

                # 追加当前块
                final_rich_text.append(chunk, style_obj)
                current_line_visual_width += chunk_width
                remaining_text = left_over

                # 如果切分导致换行（还有剩余文本），手动加换行符
                # 注意：split_text_by_cell_width 返回剩余文本意味着需要换行
                # 但我们需要在外层循环处理，这里如果还有 remaining_text，说明要进入下一次 while 循环
                # 下一次 while 循环一开始就会检测 space_left 并处理换行

        # === 主循环：处理所有 Token ===
        for t_type, t_text, t_id in self.tokens:
            is_focused = (t_id == self.active_change_id and t_id is not None and t_id != 0)

            # 准备前缀后缀
            prefix, suffix = "", ""
            if t_type == 1: prefix, suffix = "[-", "-]"
            elif t_type == 2: prefix, suffix = "{+", "+}"
            elif t_type == 31: prefix, suffix = "(<", "<)"
            elif t_type == 32: prefix, suffix = "(>", ">)"

            # 确定样式
            current_style = style_norm
            if is_focused:
                if t_type == 1: current_style = style_del_fo
                elif t_type == 2: current_style = style_add_fo
                elif t_type in [31, 32]: current_style = style_move_fo
            else:
                if t_type == 1: current_style = style_del_un
                elif t_type == 2: current_style = style_add_un
                elif t_type in [31, 32]: current_style = style_move_un

            # 组合完整文本（前缀+内容+后缀）
            # 对于 Normal 和 Context 类型，prefix/suffix 为空
            full_content = prefix + t_text + suffix

            # 处理硬换行符 (Raw Text 里的 \n)
            # 我们需要把原始文本按 \n 切开，每一段单独处理布局
            lines = full_content.split('\n')
            for i, line_content in enumerate(lines):
                if i > 0:
                    # 原始文本里有换行，意味着强制另起一行
                    final_rich_text.append("\n")
                    current_line_index += 1
                    current_line_visual_width = 0

                if line_content:
                    append_span(line_content, current_style, t_id)

        self.update(final_rich_text)

    def get_target_scroll_y(self, target_id):
        """
        直接从布局过程中生成的 Map 获取行号，精准且快速。
        """
        return self.line_y_map.get(target_id, 0)


class PathInputScreen(Screen):
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
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("请输入文件路径 (Git Repository):")
            yield Input(placeholder="/path/to/file", id="path_input")
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
        self.call_after_refresh(self.update_focus)

    def update_focus(self):
        if not self.change_ids:
            return

        target_id = self.change_ids[self.current_idx]
        viewer = self.query_one(DiffViewer)
        viewer.active_change_id = target_id

        self.title = f"{self.rel_path} [{self.current_idx + 1}/{len(self.change_ids)}]"

        # 1. 获取目标内容的起始视觉行号 (O(1) 查找)
        target_visual_y = viewer.get_target_scroll_y(target_id)

        # 2. 获取滚动容器的高度
        container = self.query_one("#scroll_container")
        container_height = container.size.height

        # 3. 计算居中滚动
        if container_height > 0:
            scroll_target = target_visual_y - (container_height // 2)
        else:
            scroll_target = target_visual_y - 10

        scroll_target = max(0, scroll_target)
        container.scroll_to(y=scroll_target, animate=False)

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
        scrollbar-gutter: stable;
    }
    """

    def on_mount(self):
        if len(sys.argv) > 1:
            target = sys.argv[1]
            if os.path.exists(target):
                abs_path = os.path.abspath(target)
                git_root = find_git_root(os.path.dirname(abs_path))
                if git_root:
                    self.push_screen(DiffScreen(git_root, abs_path))
                    return

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
