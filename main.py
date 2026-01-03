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

def parse_and_wrap_lines_robust(raw_text, width):
    """
    解析并折行。
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

    # === 2. 开始解析 ===
    wrapped_lines = []
    navigable_ids = []
    id_to_line_map = {}
    id_to_real_pos = {}

    next_change_id = 1

    pattern = re.compile(r'(\[-[\s\S]*?-\]|\{\+[\s\S]*?\+\})')
    tokens = pattern.split(raw_text)

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

    for token in tokens:
        if not token: continue

        color_type = 0
        token_id = 0
        is_current_token_change = False

        clean_text = token

        if token.startswith('[-') and token.endswith('-]'):
            color_type = 1 # Red
            is_current_token_change = True
            clean_text = ""
        elif token.startswith('{+') and token.endswith('+}'):
            color_type = 2 # Green
            is_current_token_change = True
            clean_text = token[2:-2]

        if is_current_token_change:
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

        if token_id != 0 and token_id not in id_to_real_pos:
            id_to_real_pos[token_id] = (real_row, real_col)

        if clean_text:
            update_real_pos(clean_text)

        sub_lines = token.split('\n')

        for i, sub_line in enumerate(sub_lines):
            if i > 0:
                commit_line()

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

                current_line_content.append((chunk, color_type, token_id))

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

    # 4-7: 聚焦模式 (Explicit Focused Colors)
    # 为了保证颜色对比，这里不再依赖 REVERSE，而是直接定义 (前景, 背景)
    # 你的需求：括号/符号是白色，内容是黑色，背景是原本的红/绿

    # Focused Delete (Red Background)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_RED) # 删除标记 [- -]
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_RED) # 删除内容

    # Focused Add (Green Background)
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
            current_x = 2 # Initial indentation

            # 画左侧箭头
            if line_idx == target_start_line and target_id is not None:
                stdscr.addstr(draw_y, 0, ">", curses.A_BOLD | curses.color_pair(3))

            # 遍历当前行的文本片段
            for text, color_code, segment_id in lines[line_idx]:
                # 截断超长文本
                if current_x + len(text) > max_x:
                    text_to_draw = text[:max_x - current_x - 1]
                else:
                    text_to_draw = text

                # === 核心逻辑修改：分段绘制 ===
                is_focused = (segment_id == target_id and segment_id is not None and segment_id != 0)

                if is_focused:
                    # 确定配色对
                    if color_code == 1: # Delete (Red)
                        marker_pair = curses.color_pair(4) | curses.A_BOLD
                        content_pair = curses.color_pair(5) # Black on Red
                    elif color_code == 2: # Add (Green)
                        marker_pair = curses.color_pair(6) | curses.A_BOLD
                        content_pair = curses.color_pair(7) # Black on Green
                    else:
                        marker_pair = curses.color_pair(0)
                        content_pair = curses.color_pair(0)

                    # 分割字符串进行绘制： markers 使用 marker_pair，内容使用 content_pair
                    # 逻辑：检查头部标记 -> 绘制 -> 切掉; 检查尾部标记 -> 切掉 -> 绘制内容 -> 绘制尾部

                    temp_text = text_to_draw

                    # 1. 绘制头部标记 [- 或 {+
                    if temp_text.startswith('[-') or temp_text.startswith('{+'):
                        # 标记部分 (前2个字符)
                        try:
                            stdscr.addstr(draw_y, current_x, temp_text[:2], marker_pair)
                            current_x += 2
                            temp_text = temp_text[2:]
                        except curses.error: pass

                    # 2. 检查尾部标记 -] 或 +}
                    suffix = ""
                    if temp_text.endswith('-]') or temp_text.endswith('+}'):
                        suffix = temp_text[-2:]
                        temp_text = temp_text[:-2] # 剩下的就是纯内容

                    # 3. 绘制中间的内容 (使用黑色字体)
                    if temp_text:
                        try:
                            stdscr.addstr(draw_y, current_x, temp_text, content_pair)
                            current_x += len(temp_text)
                        except curses.error: pass

                    # 4. 绘制尾部标记 (使用白色字体)
                    if suffix:
                        try:
                            stdscr.addstr(draw_y, current_x, suffix, marker_pair)
                            current_x += len(suffix)
                        except curses.error: pass

                else:
                    # === 非聚焦状态 (保持原样) ===
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

    print("--- Git 单行文件 Diff 查看器 ---")

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
