import curses
import glob
import os
import re
import subprocess
import sys

import unicodedata

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


def get_common_prefix_len(s1, s2):
    length = min(len(s1), len(s2))
    for i in range(length):
        if s1[i] != s2[i]:
            return i
    return length


def get_common_suffix_len(s1, s2):
    length = min(len(s1), len(s2))
    if length == 0: return 0
    for i in range(length):
        if s1[-(i + 1)] != s2[-(i + 1)]:
            return i
    return length


def optimize_tokens(raw_tokens):
    """
    Types:
    0: Normal
    1: Delete [- -]
    2: Add {+ +}
    31: Move Backward (< <)  [Add ... Delete]
    32: Move Forward (> >)   [Delete ... Add]
    33: Move Context (Bridge) [中间连接部分]
    """

    # === Phase 1: Global Split (细粒度化) ===
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

                if len(prefix) > 0 or len(suffix) > 0:
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

    # === Phase 2: Move Detection (移动检测 & 桥接) ===
    # 这一步我们将 List 转为可变的，直接在上面修改类型
    temp_tokens = list(split_tokens)
    processed_indices = set()

    for i in range(len(temp_tokens)):
        if i in processed_indices: continue

        curr_type, curr_text = temp_tokens[i]

        if curr_type == 1 or curr_type == 2:
            target_type = 2 if curr_type == 1 else 1
            match_index = -1

            # 向后搜索匹配项
            search_limit = 60  # 搜索范围
            for j in range(i + 1, min(len(temp_tokens), i + search_limit)):
                if j in processed_indices: continue

                scan_type, scan_text = temp_tokens[j]

                # 找到完全匹配的异类
                if scan_type == target_type and scan_text == curr_text:
                    match_index = j
                    break

            if match_index != -1:
                # 标记起点和终点
                processed_indices.add(i)
                processed_indices.add(match_index)

                move_type = 32 if curr_type == 1 else 31

                temp_tokens[i] = (move_type, curr_text)
                temp_tokens[match_index] = (move_type, curr_text)

                # === 关键修改：桥接中间部分 ===
                # 将 i 和 match_index 之间的所有 Token 标记为 Type 33 (Context)
                # 这样 ID 生成器就会把它们视为同一个 Change Block 的一部分
                for k in range(i + 1, match_index):
                    mid_type, mid_text = temp_tokens[k]
                    # 我们只修改类型，保留文本
                    # 注意：如果中间原本是 Delete/Add，这里会被覆盖为 Context，
                    # 视为移动操作的一部分（即“包含在移动块内部的杂音”）
                    temp_tokens[k] = (33, mid_text)
                    processed_indices.add(k)

    return temp_tokens


def parse_and_wrap_lines_robust(raw_text, width):
    """
    解析、优化并折行。
    """
    # 1. 剥离头部
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

    # 2. 初始拆分
    pattern = re.compile(r'(\[-[\s\S]*?-\]|\{\+[\s\S]*?\+\})')
    raw_split = pattern.split(raw_text)

    parsed_tokens = []
    for token in raw_split:
        if not token: continue
        if token.startswith('[-') and token.endswith('-]'):
            parsed_tokens.append((1, token[2:-2]))  # Delete
        elif token.startswith('{+') and token.endswith('+}'):
            parsed_tokens.append((2, token[2:-2]))  # Add
        else:
            parsed_tokens.append((0, token))  # Normal

    # 3. 智能优化
    final_tokens_data = optimize_tokens(parsed_tokens)

    # 4. 构建行数据
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

    for t_type, t_text in final_tokens_data:
        # 格式化显示文本
        display_token = t_text
        if t_type == 1:
            display_token = f"[-{t_text}-]"
        elif t_type == 2:
            display_token = f"{{+{t_text}+}}"
        elif t_type == 31:  # Move Backward
            display_token = f"(<{t_text}<)"
        elif t_type == 32:  # Move Forward
            display_token = f"(>{t_text}>)"
        # Type 33 (Context) 和 Type 0 (Normal) 不加修饰

        token_id = 0
        # 只要类型不是 0，就属于 Change 块的一部分
        # Type 33 (Context) 也是非 0，所以它会延续上一个 Change ID
        is_change = (t_type != 0)

        # ID 合并逻辑
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

        if token_id != 0 and token_id not in id_to_real_pos:
            id_to_real_pos[token_id] = (real_row, real_col)

        update_real_pos(t_text)

        # 折行
        sub_lines = display_token.split('\n')
        for i, sub_line in enumerate(sub_lines):
            if i > 0: commit_line()
            if not sub_line: continue

            remaining_text = sub_line
            while remaining_text:
                space_left = width - current_line_len

                # 如果当前行已满，先换行
                if space_left <= 0:
                    commit_line()
                    space_left = width

                # 尝试根据剩余视觉空间截取字符串
                chunk, chunk_width = cut_string_by_width(remaining_text, space_left)

                # === 关键边界情况处理 ===
                # 如果 chunk 为空，但 remaining_text 不为空，说明剩余空间连 1 个字符都放不下
                # (例如：剩1格空间，但下一个字符是中文，占2格)
                if not chunk and remaining_text:
                    commit_line()
                    space_left = width
                    # 换行后重新截取
                    chunk, chunk_width = cut_string_by_width(remaining_text, space_left)

                if token_id != 0:
                    if token_id not in id_to_line_map:
                        id_to_line_map[token_id] = len(wrapped_lines)

                current_line_content.append((chunk, t_type, token_id))

                # 【修复】这里使用真实的视觉宽度累加
                current_line_len += chunk_width

                # 推进字符串
                remaining_text = remaining_text[len(chunk):]

    if current_line_content:
        commit_line()

    return wrapped_lines, navigable_ids, id_to_line_map, id_to_real_pos


def draw_header(stdscr, max_x, rel_path, current_idx, total_changes):
    header_color = curses.color_pair(3) | curses.A_REVERSE

    progress_str = f" Change: {current_idx}/{total_changes} "
    prefix = " File: "
    available_len = max_x - len(prefix) - len(progress_str)

    display_path = rel_path
    if len(display_path) > available_len:
        if available_len > 3:
            display_path = "..." + rel_path[-(available_len - 3):]
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


def cut_string_by_width(text, max_width):
    """
    从 text 开头截取一段字符串，使其视觉宽度不超过 max_width。
    返回: (截取的字符串, 截取部分的视觉宽度)
    """
    width = 0
    for i, char in enumerate(text):
        # 使用你上一轮添加的 get_visual_width 计算单个字符宽度
        # 如果上一轮为了简单直接写在了 main 里，建议把 get_visual_width 提出来作为全局函数
        cw = get_visual_width(char)
        if width + cw > max_width:
            return text[:i], width
        width += cw
    return text, width


def get_visual_width(text):
    """计算字符串在终端显示的视觉宽度"""
    width = 0
    for char in text:
        # 判断东亚字符宽度
        # 'W' (Wide) 和 'F' (Full-width) 通常占 2 格
        if unicodedata.east_asian_width(char) in ('W', 'F'):
            width += 2
        elif char == '	':
            # 简单处理：Tab 算 8 格（如果需要完美对齐需结合 current_x，但在 diff 显示中通常够用）
            width += 8
        else:
            width += 1
    return width


def main(stdscr, repo_path, rel_path, abs_path):
    curses.start_color()
    curses.use_default_colors()

    # === 配色方案 ===
    # 1-3: 普通 / UI
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)

    # 4-7: 聚焦模式 (删除/新增)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_RED)  # 删除标记
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_RED)  # 删除内容
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_GREEN)  # 新增标记
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_GREEN)  # 新增内容

    # === 8-10: 移动 (Move) 配色 ===
    # 8: Unfocused Move (Blue text)
    curses.init_pair(8, curses.COLOR_BLUE, -1)

    # 9: Focused Move Content (White on Blue)
    curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_BLUE)

    # 10: Focused Move Symbols (Black on Blue)
    curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_BLUE)

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

        draw_header(stdscr, max_x, rel_path,
                    current_idx=(current_idx + 1 if target_id else 0),
                    total_changes=(len(change_ids) if target_id else 0))

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

                # === 核心渲染逻辑 ===
                attrs = 0

                # 1. 聚焦状态
                if is_focused:
                    # 分类处理：移动内容 vs 中间普通文本

                    if color_code == 33:
                        # === 特殊：移动块中间的普通文本 ===
                        # 即使聚焦，也按普通文本绘制 (无背景色)
                        try:
                            stdscr.addstr(draw_y, current_x, text_to_draw, curses.color_pair(0))
                            current_x += get_visual_width(text_to_draw)
                        except curses.error:
                            pass

                    elif color_code in [1, 2, 31, 32]:
                        # === 真正需要高亮的部分 (移动首尾、删除、新增) ===

                        # 定义配色对 (Marker, Content)
                        if color_code == 31 or color_code == 32:  # Move
                            marker_pair = curses.color_pair(10) | curses.A_BOLD  # Black on Blue
                            content_pair = curses.color_pair(9)  # White on Blue
                        elif color_code == 1:  # Delete
                            marker_pair = curses.color_pair(4) | curses.A_BOLD
                            content_pair = curses.color_pair(5)
                        elif color_code == 2:  # Add
                            marker_pair = curses.color_pair(6) | curses.A_BOLD
                            content_pair = curses.color_pair(7)

                        temp_text = text_to_draw

                        # 绘制前缀: [-, {+, (<, (>
                        for p in ['[-', '{+', '(<', '(>']:
                            if temp_text.startswith(p):
                                try:
                                    stdscr.addstr(draw_y, current_x, temp_text[:2], marker_pair)
                                    current_x += 2
                                    temp_text = temp_text[2:]
                                except curses.error:
                                    pass
                                break

                        # 绘制后缀: -], +}, <), >)
                        suffix = ""
                        for s in ['-]', '+}', '<)', '>)']:
                            if temp_text.endswith(s):
                                suffix = s
                                temp_text = temp_text[:-len(s)]
                                break

                        # 绘制内容
                        if temp_text:
                            try:
                                stdscr.addstr(draw_y, current_x, temp_text, content_pair)
                                current_x += get_visual_width(temp_text)
                            except curses.error:
                                pass

                        # 绘制后缀
                        if suffix:
                            try:
                                stdscr.addstr(draw_y, current_x, suffix, marker_pair)
                                current_x += get_visual_width(suffix)
                            except curses.error:
                                pass

                    else:
                        # 理论上不应该进入这里，除非是 Type 0 且 token_id != 0
                        try:
                            stdscr.addstr(draw_y, current_x, text_to_draw, curses.color_pair(0))
                            current_x += get_visual_width(text_to_draw)
                        except curses.error:
                            pass

                # 2. 非聚焦状态
                else:
                    if color_code == 31 or color_code == 32:  # Move Text
                        attrs = curses.color_pair(8) | curses.A_BOLD  # Blue
                    elif color_code == 33:  # Move Context
                        attrs = curses.color_pair(0)  # Normal
                    else:
                        attrs = curses.color_pair(color_code)

                    try:
                        stdscr.addstr(draw_y, current_x, text_to_draw, attrs)
                        current_x += get_visual_width(text_to_draw)
                    except curses.error:
                        pass

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
    print("--- Git 单行文件 Diff 查看器 (Merged Move Block) ---")
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
                    print("\n退出。");
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
                break
            else:
                cli_arg_used = False; pass
        except Exception as e:
            print(f"程序运行出错: {e}"); break
