import sys
import os
import subprocess
import curses
import re
import glob

# 尝试导入 readline 以支持 Tab 补全
try:
    import readline
except ImportError:
    readline = None

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
        # 保持你要求的正则不变
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

def get_common_prefix_len(s1, s2):
    """计算两个字符串的公共前缀长度"""
    length = min(len(s1), len(s2))
    for i in range(length):
        if s1[i] != s2[i]:
            return i
    return length

def get_common_suffix_len(s1, s2):
    """计算两个字符串的公共后缀长度"""
    length = min(len(s1), len(s2))
    if length == 0: return 0
    for i in range(length):
        if s1[-(i+1)] != s2[-(i+1)]:
            return i
    return length

def optimize_tokens(raw_tokens):
    """
    核心优化逻辑：合并相邻的 Delete 和 Add，提取公共首尾。
    输入 raw_tokens: list of (type, text)
       type: 0=Normal, 1=Delete, 2=Add
    """
    optimized = []
    i = 0
    while i < len(raw_tokens):
        curr_type, curr_text = raw_tokens[i]

        # 检查是否是 Delete (1) 且下一个是 Add (2)
        if curr_type == 1 and i + 1 < len(raw_tokens):
            next_type, next_text = raw_tokens[i+1]

            if next_type == 2:
                # 发现相邻的 Delete + Add，尝试提取公共部分
                p_len = get_common_prefix_len(curr_text, next_text)
                s_len = get_common_suffix_len(curr_text, next_text)

                # 防止重叠：如果前缀+后缀超过了原本长度，优先保留前缀，减少后缀长度
                max_len = min(len(curr_text), len(next_text))
                if p_len + s_len > max_len:
                    s_len = max_len - p_len

                # 只有当确实有公共部分时才拆分
                if p_len > 0 or s_len > 0:
                    prefix = curr_text[:p_len]
                    suffix = curr_text[len(curr_text)-s_len:] if s_len > 0 else ""

                    mid_del = curr_text[p_len : len(curr_text)-s_len]
                    mid_add = next_text[p_len : len(next_text)-s_len]

                    # 1. 添加公共前缀 (Normal)
                    if prefix:
                        optimized.append((0, prefix))

                    # 2. 添加中间差异部分 (Delete & Add)
                    if mid_del:
                        optimized.append((1, mid_del))
                    if mid_add:
                        optimized.append((2, mid_add))

                    # 3. 添加公共后缀 (Normal)
                    if suffix:
                        optimized.append((0, suffix))

                    # 跳过下一个 token，因为已经处理了
                    i += 2
                    continue

        # 如果没有触发合并逻辑，直接添加
        optimized.append((curr_type, curr_text))
        i += 1

    return optimized

def parse_and_wrap_lines_robust(raw_text, width):
    """
    解析、优化并折行。
    """
    # === 1. 预处理：剥离 Git 头部元数据 ===
    header_patterns = [
        r'^diff --git .*(\n|$)',
        r'^index .*(\n|$)',
        r'^--- .*(\n|$)',
        r'^\+\+\+ .*(\n|$)',
        r'^@@ .*? @@(\n|$)'
    ]
    for pat in header_patterns:
        raw_text = re.sub(pat, '', raw_text, flags=re.MULTILINE)
    if raw_text.startswith('\n'):
        raw_text = raw_text.lstrip('\n')

    # === 2. 初始 Token化 (Type, CleanText) ===
    # 将原始字符串转化为结构化列表，不再携带括号
    pattern = re.compile(r'(\[-[\s\S]*?-\]|\{\+[\s\S]*?\+\})')
    raw_split = pattern.split(raw_text)

    parsed_tokens = []
    for token in raw_split:
        if not token: continue

        if token.startswith('[-') and token.endswith('-]'):
            # Type 1: Delete
            parsed_tokens.append((1, token[2:-2]))
        elif token.startswith('{+') and token.endswith('+}'):
            # Type 2: Add
            parsed_tokens.append((2, token[2:-2]))
        else:
            # Type 0: Normal
            parsed_tokens.append((0, token))

    # === 3. 执行优化逻辑 (提取公共首尾) ===
    final_tokens_data = optimize_tokens(parsed_tokens)

    # === 4. 开始构建显示行和 ID ===
    wrapped_lines = []
    navigable_ids = []
    id_to_line_map = {}
    id_to_real_pos = {}

    next_change_id = 1
    current_line_content = []
    current_line_len = 0

    last_was_change = False
    current_active_id = 0

    real_row = 1
    real_col = 1

    def commit_line():
        nonlocal current_line_content, current_line_len
        wrapped_lines.append(current_line_content)
        current_line_content = []
        current_line_len = 0

    def update_real_pos(text_content):
        nonlocal real_row, real_col
        newlines = text_content.count('\n')
        if newlines > 0:
            real_row += newlines
            last_nl_index = text_content.rfind('\n')
            real_col = len(text_content) - last_nl_index
        else:
            real_col += len(text_content)

    # 遍历优化后的 token 列表
    for t_type, t_text in final_tokens_data:
        # 重新包装为显示文本 (带括号)
        # 这样 main 函数里的渲染逻辑 (检测 [- ... ]) 才能继续工作
        display_token = t_text
        if t_type == 1:
            display_token = f"[-{t_text}-]"
        elif t_type == 2:
            display_token = f"{{+{t_text}+}}"

        token_id = 0
        is_change = (t_type != 0)

        # ID 分配与合并逻辑
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

        # 记录真实坐标 (ID 对应位置)
        if token_id != 0 and token_id not in id_to_real_pos:
            id_to_real_pos[token_id] = (real_row, real_col)

        # 更新坐标 (普通文本和新增文本推进光标)
        if t_type == 0 or t_type == 2:
            update_real_pos(t_text)

        # 折行逻辑
        sub_lines = display_token.split('\n')
        for i, sub_line in enumerate(sub_lines):
            if i > 0: commit_line()
            if not sub_line: continue

            idx = 0
            while idx < len(sub_line):
                space_left = width - current_line_len
                if space_left <= 0:
                    commit_line()
                    space_left = width

                chunk = sub_line[idx : idx + space_left]

                if token_id != 0:
                    if token_id not in id_to_line_map:
                        id_to_line_map[token_id] = len(wrapped_lines)

                current_line_content.append((chunk, t_type, token_id))
                current_line_len += len(chunk)
                idx += len(chunk)

    if current_line_content:
        commit_line()

    return wrapped_lines, navigable_ids, id_to_line_map, id_to_real_pos

def draw_header(stdscr, max_x, rel_path, current_idx, total_changes):
    """绘制头部 (2行)"""
    header_color = curses.color_pair(3) | curses.A_REVERSE

    progress_str = f" Change: {current_idx}/{total_changes} "
    prefix = " File: "
    available_len = max_x - len(prefix) - len(progress_str)

    display_path = rel_path
    if len(display_path) > available_len:
        if available_len > 3:
            display_path = "..." + rel_path[-(available_len-3):]
        else:
            display_path = ""

    line1 = f"{prefix}{display_path}"
    padding = " " * (max_x - len(line1) - len(progress_str))
    full_line1 = line1 + padding + progress_str

    line2 = " [←/→]:Prev/Next  [↑/↓]:Scroll  [Esc]:New File  [q]:Quit "
    if len(line2) < max_x:
        line2 += " " * (max_x - len(line2))
    else:
        line2 = line2[:max_x]

    try:
        stdscr.addstr(0, 0, full_line1[:max_x], header_color)
        stdscr.addstr(1, 0, line2[:max_x], header_color)
    except curses.error:
        pass

def draw_footer_status(stdscr, max_y, max_x, real_pos):
    """绘制底部状态栏"""
    if not real_pos:
        pos_str = " Pos: N/A "
    else:
        pos_str = f" Ln {real_pos[0]}, Col {real_pos[1]} "

    status_color = curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD

    try:
        stdscr.move(max_y - 1, 0)
        stdscr.clrtoeol()
        start_x = max_x - len(pos_str)
        if start_x >= 0:
            stdscr.addstr(max_y - 1, start_x, pos_str, status_color)
    except curses.error:
        pass

def main(stdscr, repo_path, rel_path, abs_path):
    curses.start_color()
    curses.use_default_colors()

    # === 定义配色方案 ===
    # 1-3: 普通模式 (非聚焦)
    curses.init_pair(1, curses.COLOR_RED, -1)   # Red text (删除)
    curses.init_pair(2, curses.COLOR_GREEN, -1) # Green text (新增)
    curses.init_pair(3, curses.COLOR_CYAN, -1)  # UI

    # 4-7: 聚焦模式
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_RED) # 删除标记 [- -]
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_RED) # 删除内容
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_GREEN) # 新增标记 {+ +}
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_GREEN) # 新增内容

    curses.curs_set(0)

    stdscr.addstr(0, 0, "Loading diff...", curses.A_BOLD)
    stdscr.refresh()

    raw_diff = get_git_diff(repo_path, rel_path)

    if not raw_diff.strip():
        stdscr.clear()
        stdscr.addstr(1, 1, f"No changes found for: {rel_path}")
        stdscr.addstr(2, 1, "Press 'q' to exit, 'Esc' to new file.")
        while True:
            k = stdscr.getch()
            if k == ord('q'): return False
            if k == 27: return True
        return False

    max_y, max_x = stdscr.getmaxyx()
    content_width = max_x - 3

    lines, change_ids, id_map, real_pos_map = parse_and_wrap_lines_robust(raw_diff, content_width)

    if not change_ids:
        change_ids = [None]
        id_map = {None: 0}

    current_idx = 0
    scroll_offset = 0
    auto_scroll = True

    header_height = 2
    footer_height = 1

    while True:
        stdscr.clear()
        max_y, max_x = stdscr.getmaxyx()

        page_size = max_y - header_height - footer_height
        if page_size < 1: page_size = 1

        target_id = change_ids[current_idx]
        target_start_line = id_map.get(target_id, 0)

        current_real_pos = real_pos_map.get(target_id)

        if auto_scroll:
            half_screen = page_size // 2
            scroll_offset = target_start_line - half_screen

        if scroll_offset < 0: scroll_offset = 0
        if scroll_offset > len(lines) - page_size: scroll_offset = len(lines) - page_size
        if scroll_offset < 0: scroll_offset = 0

        # Draw Header
        display_idx = current_idx + 1 if target_id is not None else 0
        total_changes = len(change_ids) if target_id is not None else 0
        draw_header(stdscr, max_x, rel_path, display_idx, total_changes)

        # Draw Content
        for i in range(page_size):
            line_idx = scroll_offset + i
            if line_idx >= len(lines): break

            draw_y = i + header_height
            current_x = 2

            if line_idx == target_start_line and target_id is not None:
                stdscr.addstr(draw_y, 0, ">", curses.A_BOLD | curses.color_pair(3))

            for text, color_code, segment_id in lines[line_idx]:
                if current_x + len(text) > max_x:
                    text_to_draw = text[:max_x - current_x - 1]
                else:
                    text_to_draw = text

                is_focused = (segment_id == target_id and segment_id is not None and segment_id != 0)

                if is_focused:
                    if color_code == 1:
                        marker_pair = curses.color_pair(4) | curses.A_BOLD
                        content_pair = curses.color_pair(5)
                    elif color_code == 2:
                        marker_pair = curses.color_pair(6) | curses.A_BOLD
                        content_pair = curses.color_pair(7)
                    else:
                        marker_pair = curses.color_pair(0)
                        content_pair = curses.color_pair(0)

                    temp_text = text_to_draw

                    if temp_text.startswith('[-') or temp_text.startswith('{+'):
                        try:
                            stdscr.addstr(draw_y, current_x, temp_text[:2], marker_pair)
                            current_x += 2
                            temp_text = temp_text[2:]
                        except curses.error: pass

                    suffix = ""
                    if temp_text.endswith('-]') or temp_text.endswith('+}'):
                        suffix = temp_text[-2:]
                        temp_text = temp_text[:-2]

                    if temp_text:
                        try:
                            stdscr.addstr(draw_y, current_x, temp_text, content_pair)
                            current_x += len(temp_text)
                        except curses.error: pass

                    if suffix:
                        try:
                            stdscr.addstr(draw_y, current_x, suffix, marker_pair)
                            current_x += len(suffix)
                        except curses.error: pass

                else:
                    attrs = curses.color_pair(color_code)
                    try:
                        stdscr.addstr(draw_y, current_x, text_to_draw, attrs)
                        current_x += len(text_to_draw)
                    except curses.error:
                        pass

        # Draw Footer
        draw_footer_status(stdscr, max_y, max_x, current_real_pos)

        stdscr.refresh()

        key = stdscr.getch()

        if key == ord('q'):
            return False
        elif key == 27:
            return True

        elif key == curses.KEY_RIGHT or key == ord('l'):
            if current_idx < len(change_ids) - 1:
                current_idx += 1
                auto_scroll = True
            else:
                curses.beep()
        elif key == curses.KEY_LEFT or key == ord('h'):
            if current_idx > 0:
                current_idx -= 1
                auto_scroll = True
            else:
                curses.beep()
        elif key == curses.KEY_DOWN or key == ord('j'):
            scroll_offset += 1
            auto_scroll = False
        elif key == curses.KEY_UP or key == ord('k'):
            scroll_offset -= 1
            auto_scroll = False
        elif key == curses.KEY_NPAGE:
            scroll_offset += page_size
            auto_scroll = False
        elif key == curses.KEY_PPAGE:
            scroll_offset -= page_size
            auto_scroll = False

if __name__ == "__main__":
    first_run = True
    cli_arg_used = False

    if readline:
        readline.parse_and_bind("tab: complete")
        readline.set_completer(lambda t, s: (glob.glob(t + '*') + [None])[s])

    print("--- Git 单行文件 Diff 查看器 (智能优化版) ---")

    while True:
        target = ""
        if first_run and len(sys.argv) >= 2:
            target = sys.argv[1]
            cli_arg_used = True
        else:
            while True:
                try:
                    if not first_run: print("")
                    raw_input = input("请输入文件路径: ").strip()
                    target = raw_input.strip("'\"")

                    if not target: continue
                    if os.path.exists(target): break
                    print(f"错误: 文件 '{target}' 不存在，请重试。")
                except KeyboardInterrupt:
                    print("\n退出。")
                    sys.exit(0)

        first_run = False

        abs_file_path = os.path.abspath(target)
        git_root = find_git_root(os.path.dirname(abs_file_path))

        if not git_root:
            print(f"错误: 文件 '{target}' 不在 Git 仓库中。")
            if cli_arg_used:
                sys.exit(1)
            else:
                continue

        rel_path = os.path.relpath(abs_file_path, git_root)

        try:
            should_continue = curses.wrapper(main, git_root, rel_path, abs_file_path)

            if not should_continue:
                print("Bye!")
                break
            else:
                cli_arg_used = False
                pass

        except Exception as e:
            print(f"程序运行出错: {e}")
            break
