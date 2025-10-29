import os
import shutil
import click
import hashlib
import gc
import psutil
import concurrent.futures
from functools import partial
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer, get_lexer_by_name
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound
import markdown2
from jinja2 import Environment, FileSystemLoader

# 配置项
DEFAULT_OUTPUT_DIR = "./output"
IGNORE_PATTERNS = [
    # 目录（以/结尾表示目录）
    ".git/", ".github/", "node_modules/", "__pycache__/",
    ".vscode/", ".cache/", "build/", ".sdk/", "dist/", "bin/",
    "pc-bios/", "rust/target/",
    # 文件扩展名（以.开头）
    ".bin", ".rom", ".bz2", ".gz", ".zip", ".tar", ".7z",
    ".out", ".o", ".so", ".dll", ".pyc", ".pyo", ".patch",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
    ".pdf", ".docx", ".xlsx", ".pptx",
    # 特定文件名
    ".gdb_history", ".clang-format", ".git-submodule-status",
    "Makefile", "makefile", "README", "LICENSE"
]
HIGHLIGHT_THEME = "github-dark"  # 暗色主题
TEMPLATE_DIR = "./templates"
MAX_PATH_LENGTH = 255
BATCH_SIZE = 200  # 每批处理的文件数量
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB，大于此的文件将被跳过
MERGED_FILE_SIZE_LIMIT = 9.5 * 1024 * 1024  # 合并文件的大小限制9.5MiB
MEMORY_THRESHOLD = 0.85  # 内存使用阈值（80%）
MAX_WORKERS = None  # 最大工作线程数
MAX_DIRECTORY_DEPTH = 5  # 最内层文件夹的深度，此深度的文件夹及其所有子内容将被合并
TEXT_CHARS = bytes([7, 8, 9, 10, 12, 13, 27]) + \
    bytes(range(0x20, 0x100))  # 文本文件特征字节


def get_optimal_workers():
    """根据系统资源计算最佳工作线程数"""
    cpu_count = os.cpu_count() or 4
    mem_gb = psutil.virtual_memory().total / (1024 **3)
    
    # 内存越多，允许的线程数越多
    if mem_gb >= 16:
        return max(8, cpu_count * 4)
    elif mem_gb >= 8:
        return max(4, cpu_count * 2)
    else:
        return max(2, cpu_count)


def get_directory_depth(path):
    """获取目录深度"""
    path_parts = [part for part in path.split(os.sep) if part]
    return len(path_parts)


def is_target_directory(path, repo_root):
    """判断是否为目标深度的目录（需要合并其所有内容）"""
    if not os.path.isdir(path):
        return False
    
    # 计算相对于仓库根目录的深度
    rel_path = os.path.relpath(path, repo_root)
    depth = get_directory_depth(rel_path)
    
    # 仅当目录深度等于MAX_DIRECTORY_DEPTH时视为目标目录
    return depth == MAX_DIRECTORY_DEPTH


def collect_all_files_in_directory(dir_path):
    """收集目录中所有文件（包括子目录中的文件）"""
    all_files = []
    stack = [dir_path]
    
    while stack:
        current_path = stack.pop()
        try:
            with os.scandir(current_path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            # 递归处理子目录
                            if not should_ignore(entry.path):
                                stack.append(entry.path)
                        else:
                            # 收集文件
                            if not should_ignore(entry.path):
                                file_size = entry.stat().st_size
                                if file_size <= MAX_FILE_SIZE:
                                    all_files.append((entry.path, file_size))
                                else:
                                    rel_path = os.path.relpath(entry.path, dir_path)
                                    click.echo(f"⏭️ 跳过过大文件 ({file_size//1024//1024}MB): {rel_path}")
                    except Exception as e:
                        click.echo(f"⚠️ 访问{entry.path}时出错: {str(e)[:30]}")
                        continue
        except PermissionError:
            click.echo(f"🚫 没有权限访问目录: {current_path}")
            continue
        except Exception as e:
            click.echo(f"⚠️ 处理目录{current_path}时出错: {str(e)[:30]}")
            continue
    
    return all_files


def collect_target_directories(repo_path):
    """收集所有达到目标深度的目录"""
    target_dirs = {}
    stack = [repo_path]

    while stack:
        current_dir = stack.pop()
        try:
            # 检查当前目录是否为目标深度目录
            if is_target_directory(current_dir, repo_path):
                # 收集该目录下的所有文件（包括子目录）
                all_files = collect_all_files_in_directory(current_dir)
                if all_files:  # 只保留有可处理文件的目录
                    target_dirs[current_dir] = all_files
                continue  # 目标目录不再递归处理其子目录
            
            # 继续扫描更深的目录
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False) and not should_ignore(entry.path):
                            stack.append(entry.path)
                    except Exception as e:
                        click.echo(f"⚠️ 访问{entry.path}时出错: {str(e)[:30]}")
                        continue
        except PermissionError:
            click.echo(f"🚫 没有权限访问目录: {current_dir}")
            continue
        except Exception as e:
            click.echo(f"⚠️ 处理目录{current_dir}时出错: {str(e)[:30]}")
            continue

    return target_dirs


def is_binary_file(file_path, sample_size=1024):
    """判断文件是否为二进制文件"""
    if not os.path.isfile(file_path) or os.path.islink(file_path):
        return True

    try:
        with open(file_path, 'rb') as f:
            sample = f.read(sample_size)

        if not sample:
            return False

        return bool(sample.translate(None, TEXT_CHARS))
    except Exception:
        return True


def get_memory_usage():
    """获取当前进程内存使用率（百分比）"""
    return psutil.Process(os.getpid()).memory_percent()


def wait_for_memory():
    """等待内存使用率降至阈值以下"""
    while get_memory_usage() > MEMORY_THRESHOLD * 100:
        current_usage = get_memory_usage()
        click.echo(f"⚠️ 内存使用率过高 ({current_usage:.1f}%), 等待释放...")
        gc.collect()  # 强制垃圾回收
        psutil.sleep(1)


def should_ignore(path):
    """判断路径是否应该被忽略"""
    path_lower = path.lower()
    
    # 检查文件扩展名
    for pattern in IGNORE_PATTERNS:
        if pattern.startswith('.') and len(pattern) > 1 and path_lower.endswith(pattern):
            return True
    
    # 检查目录
    path_parts = path.split(os.sep)
    for pattern in IGNORE_PATTERNS:
        if pattern.endswith('/'):
            dir_name = pattern.rstrip('/')
            if dir_name in path_parts:
                return True
    
    # 检查文件名
    filename = os.path.basename(path)
    for pattern in IGNORE_PATTERNS:
        if not pattern.startswith('.') and not pattern.endswith('/') and filename == pattern:
            return True
    
    # 检查是否为二进制文件
    return os.path.isfile(path) and is_binary_file(path)


def get_safe_formatter(theme, output_format):
    """获取安全的代码格式化器并带有缓存机制"""
    cache_key = f"{theme}_{output_format}"
    if not hasattr(get_safe_formatter, "_cache"):
        get_safe_formatter._cache = {}

    if cache_key not in get_safe_formatter._cache:
        try:
            formatter = HtmlFormatter(style=theme, linenos=True)
        except ClassNotFound:
            click.warning(f"⚠️ 高亮主题'{theme}'不存在，使用默认主题")
            formatter = HtmlFormatter(style="default", linenos=True)
        get_safe_formatter._cache[cache_key] = formatter

    return get_safe_formatter._cache[cache_key]


def validate_local_repo(local_path):
    """验证本地仓库路径的有效性"""
    local_path = os.path.abspath(local_path)
    if not os.path.exists(local_path):
        raise ValueError(f"路径不存在: {local_path}")
    if not os.path.isdir(local_path):
        raise ValueError(f"不是目录: {local_path}")
    return local_path, os.path.basename(local_path)


def shorten_long_path(original_path, max_length, output_format):
    """缩短过长的文件路径，保留原始扩展名并添加新格式后缀"""
    if len(original_path) <= max_length:
        return original_path

    dir_name = os.path.dirname(original_path)
    file_name = os.path.basename(original_path)

    # 保留原始文件名和扩展名，只在末尾添加格式后缀
    hash_suffix = hashlib.md5(original_path.encode()).hexdigest()[:6]
    base_name = os.path.splitext(file_name)[0]
    original_ext = os.path.splitext(file_name)[1]
    new_file_name = f"{base_name}_{hash_suffix}{original_ext}.{output_format}"

    return os.path.join(dir_name, new_file_name)


def estimate_content_size(content, output_format):
    """估计内容写入磁盘时的字节大小"""
    if output_format == 'html':
        return len(content.encode('utf-8')) * 1.2  # 粗略估计HTML的额外开销
    return len(content.encode('utf-8'))  # Markdown大致是1:1


def read_file_content(file_path):
    """读取文件内容，使用优化的分块读取"""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin-1') as f:
            return f.read()


def process_target_directory(dir_path, repo_root, output_dir, formatter, output_format):
    """处理目标深度目录，合并其所有文件（包括子目录中的文件）"""
    try:
        rel_dir_path = os.path.relpath(dir_path, repo_root)
        dir_name = os.path.basename(dir_path)
        
        # 获取该目录下的所有文件（已在收集阶段完成）
        files_with_size = collect_all_files_in_directory(dir_path)
        if not files_with_size:
            return (False, f"📂 目标目录{rel_dir_path}中没有可处理的文件")

        # 按文件大小排序以优化拆分
        files_with_size.sort(key=lambda x: x[1])
        files_in_dir = [f[0] for f in files_with_size]
        
        # 生成基础输出路径
        output_rel_path = os.path.dirname(rel_dir_path)
        base_output_file_name = f"{dir_name}"
        base_output_path = os.path.join(output_dir, output_rel_path, base_output_file_name)
        os.makedirs(os.path.dirname(base_output_path), exist_ok=True)

        # 准备内容部分
        sections = []
        current_section = {"content": "", "size_estimate": 0, "files_included": []}
        
        if output_format == 'html':
            section_header = f"<h1>目录内容: {rel_dir_path}（第{{part_number}}部分）</h1>\n"
            highlight_css = formatter.get_style_defs(".highlight")
        else:  # markdown
            section_header = f"# 目录内容: {rel_dir_path}（第{{part_number}}部分）\n\n"

        # 计算标题大小
        header_size = estimate_content_size(section_header.replace("{{part_number}}", "1"), output_format)
        
        # 处理每个文件并添加到部分中
        for file_path in files_in_dir:
            # 计算文件相对于目标目录的路径，保留完整的子目录结构信息
            rel_file_path = os.path.relpath(file_path, dir_path)
            full_rel_path = os.path.join(rel_dir_path, rel_file_path)
            
            # 读取文件内容
            content = read_file_content(file_path)

            # 处理单个文件内容
            ext = os.path.splitext(file_path)[1].lower()
            file_ext = ext.lstrip('.') if ext else 'txt'
            file_content = ""

            if output_format == 'html':
                # 添加文件路径标题（包含完整的子目录结构）
                file_content += f"<h2>文件路径: {full_rel_path}</h2>\n"
                
                if ext == ".md":
                    # 转换Markdown为HTML
                    html_content = markdown2.markdown(
                        content,
                        extras=["fenced-code-blocks", "tables"]
                    )
                    file_content += f"<div class='file-content'>{html_content}</div>\n"
                else:
                    # 转换代码文件为带高亮的HTML
                    try:
                        lexer = get_lexer_for_filename(file_path)
                    except Exception:
                        lexer = TextLexer()
                    html_content = highlight(content, lexer, formatter)
                    file_content += f"<div class='file-content'>{html_content}</div>\n"

            else:  # markdown
                # 添加文件路径标题（包含完整的子目录结构）
                file_content += f"## 文件路径: {full_rel_path}\n\n"
                
                if ext == ".md":
                    # 保留Markdown文件的原始内容
                    file_content += f"{content}\n\n"
                else:
                    # 生成Markdown代码块
                    try:
                        lexer = get_lexer_for_filename(file_path)
                        lexer_name = lexer.aliases[0] if lexer.aliases else lexer.name.lower()
                    except Exception:
                        try:
                            lexer = get_lexer_by_name(file_ext)
                            lexer_name = file_ext
                        except Exception:
                            lexer_name = 'text'

                    # 转义代码中的反引号
                    escaped_content = content.replace('`', '\\`')
                    file_content += f"``` {lexer_name}\n{escaped_content}\n```\n\n"

            # 估计大小并管理部分
            file_content_size = estimate_content_size(file_content, output_format)
            
            if current_section["size_estimate"] + file_content_size > MERGED_FILE_SIZE_LIMIT:
                if current_section["size_estimate"] > 0:
                    sections.append(current_section)
                current_section = {
                    "content": file_content,
                    "size_estimate": header_size + file_content_size,
                    "files_included": [full_rel_path]
                }
            else:
                current_section["content"] += file_content
                current_section["size_estimate"] += file_content_size
                current_section["files_included"].append(full_rel_path)

        if current_section["size_estimate"] > 0:
            sections.append(current_section)

        # 写入所有部分
        results = []
        for i, section in enumerate(sections, 1):
            if len(sections) > 1:
                output_file_name = f"{base_output_file_name}_part{i}.{output_format}"
            else:
                output_file_name = f"{base_output_file_name}.{output_format}"
                
            output_path = os.path.join(output_dir, output_rel_path, output_file_name)
            output_path = shorten_long_path(output_path, MAX_PATH_LENGTH, output_format)

            if output_format == 'html':
                formatted_header = section_header.replace("{{part_number}}", str(i) if len(sections) > 1 else "1")
                final_content = file_template.render(
                    title=f"{rel_dir_path}（第{i}部分）" if len(sections) > 1 else rel_dir_path,
                    rel_path=rel_dir_path,
                    content=formatted_header + section["content"],
                    highlight_css=highlight_css
                )
            else:
                formatted_header = section_header.replace("{{part_number}}", str(i) if len(sections) > 1 else "1")
                final_content = formatted_header + section["content"]

            # 写入文件
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(final_content)

            results.append((output_path, section["files_included"]))

        if len(sections) > 1:
            message = f"🟢 目标目录拆分为{len(sections)}个部分（{output_format}）: {rel_dir_path}"
        else:
            message = f"🟢 目标目录合并为{output_format}: {rel_dir_path}"

        return (True, message, results)

    except Exception as e:
        rel_path = os.path.relpath(dir_path, repo_root)
        return (False, f"❌ 处理目录{rel_path}失败: {str(e)[:50]}", None)


def process_single_file(file_path, repo_root, output_dir, formatter, output_format):
    """处理非目标深度目录中的单个文件"""
    try:
        # 跳过过大文件
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            rel_path = os.path.relpath(file_path, repo_root)
            return (False, f"⏭️ 跳过过大文件 ({file_size//1024//1024}MB): {rel_path}")

        # 获取相对路径
        rel_path = os.path.relpath(file_path, repo_root)

        # 检查是否应该被忽略
        if should_ignore(rel_path) or should_ignore(file_path):
            if is_binary_file(file_path):
                return (False, f"🔇 忽略二进制文件: {rel_path}")
            else:
                return (False, f"🔇 忽略文件: {rel_path}")

        # 生成输出文件路径
        file_name = os.path.basename(rel_path)
        new_file_name = f"{file_name}.{output_format}"
        output_rel_path = os.path.join(
            os.path.dirname(rel_path), new_file_name)
        output_path = os.path.join(output_dir, output_rel_path)
        output_path = shorten_long_path(
            output_path, MAX_PATH_LENGTH, output_format)

        # 创建输出目录
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 读取文件内容
        content = read_file_content(file_path)

        # 根据文件类型和输出格式处理内容
        ext = os.path.splitext(file_path)[1].lower()
        file_ext = ext.lstrip('.') if ext else 'txt'

        if output_format == 'html':
            if ext == ".md":
                # 转换Markdown为HTML
                html_content = markdown2.markdown(
                    content,
                    extras=["fenced-code-blocks", "tables"]
                )
                highlight_css = ""
            else:
                # 转换代码文件为带高亮的HTML
                try:
                    lexer = get_lexer_for_filename(file_path)
                except Exception:
                    lexer = TextLexer()
                html_content = highlight(content, lexer, formatter)
                highlight_css = formatter.get_style_defs(".highlight")

            # 使用模板生成完整HTML
            final_content = file_template.render(
                title=rel_path,
                rel_path=rel_path,
                content=html_content,
                highlight_css=highlight_css
            )

        else:  # markdown
            if ext == ".md":
                # 保留Markdown文件的原始内容，添加标题
                final_content = f"# 文件路径: {rel_path}\n\n{content}"
            else:
                # 生成Markdown代码块
                try:
                    lexer = get_lexer_for_filename(file_path)
                    lexer_name = lexer.aliases[0] if lexer.aliases else lexer.name.lower()
                except Exception:
                    try:
                        lexer = get_lexer_by_name(file_ext)
                        lexer_name = file_ext
                    except Exception:
                        lexer_name = 'text'

                # 转义代码中的反引号
                escaped_content = content.replace('`', '\\`')
                final_content = f"# 文件路径: {rel_path}\n\n``` {lexer_name}\n{escaped_content}\n```\n"

        # 写入输出文件
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        return (True, f"🟢 转换为{output_format}: {rel_path}")

    except Exception as e:
        rel_path = os.path.relpath(file_path, repo_root)
        return (False, f"❌ 处理文件{rel_path}失败: {str(e)[:50]}")


def collect_files_and_dirs_to_process(repo_path):
    """收集所有需要处理的文件和目标目录"""
    target_dirs = collect_target_directories(repo_path)
    non_target_files = []
    stack = [repo_path]

    # 标记所有目标目录，避免重复处理其文件
    target_dir_set = set(target_dirs.keys())

    while stack:
        current_dir = stack.pop()
        # 如果是目标目录则跳过
        if current_dir in target_dir_set:
            continue
            
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not should_ignore(entry.path):
                                stack.append(entry.path)
                        else:
                            if not should_ignore(entry.path):
                                non_target_files.append(entry.path)
                    except Exception as e:
                        click.echo(f"⚠️ 访问{entry.path}时出错: {str(e)[:30]}")
                        continue
        except PermissionError:
            click.echo(f"🚫 没有权限访问目录: {current_dir}")
            continue
        except Exception as e:
            click.echo(f"⚠️ 处理目录{current_dir}时出错: {str(e)[:30]}")
            continue

    click.echo(f"📊 发现{len(non_target_files)}个常规文件和{len(target_dirs)}个目标深度目录需要处理")
    return non_target_files, target_dirs, target_dir_set


def generate_index(repo_root, output_dir, repo_name, non_target_files, target_dirs, target_dir_set, output_format):
    """生成目录索引页"""
    file_tree = {"__files__": [], "__dirs__": []}  # 根节点包含文件和目录

    # 添加常规文件
    for file_path in non_target_files:
        rel_path = os.path.relpath(file_path, repo_root)
        file_name = os.path.basename(rel_path)
        dir_path = os.path.dirname(rel_path)

        # 构建目录树结构
        current_node = file_tree
        dir_parts = dir_path.split(os.sep) if dir_path != '.' else []

        for part in dir_parts:
            if not part:
                continue
            if part not in current_node:
                current_node[part] = {"__files__": [], "__dirs__": []}
            current_node = current_node[part]

        # 添加文件信息
        output_file_name = f"{file_name}.{output_format}"
        output_rel_path = os.path.join(dir_path, output_file_name).replace(os.sep, "/")
        current_node["__files__"].append({
            "name": file_name,
            "url": output_rel_path,
            "formatted_name": output_file_name
        })

    # 添加目标目录
    for dir_path, files_in_dir in target_dirs.items():
        rel_dir_path = os.path.relpath(dir_path, repo_root)
        dir_name = os.path.basename(rel_dir_path)
        parent_dir = os.path.dirname(rel_dir_path)

        # 构建目录树结构
        current_node = file_tree
        dir_parts = parent_dir.split(os.sep) if parent_dir != '.' else []

        for part in dir_parts:
            if not part:
                continue
            if part not in current_node:
                current_node[part] = {"__files__": [], "__dirs__": []}
            current_node = current_node[part]

        # 计算总大小以确定是否被拆分
        total_size = sum(size for _, size in files_in_dir if size <= MAX_FILE_SIZE)
        output_files = []
        
        # 确定输出文件名
        if total_size > MERGED_FILE_SIZE_LIMIT:
            # 估计分块数量（粗略估计）
            estimated_parts = max(1, int(total_size / MERGED_FILE_SIZE_LIMIT) + 1)
            for i in range(1, estimated_parts + 1):
                output_file_name = f"{dir_name}_part{i}.{output_format}"
                output_rel_path = os.path.join(parent_dir, output_file_name).replace(os.sep, "/")
                output_files.append({
                    "name": f"{dir_name}_part{i}",
                    "url": output_rel_path,
                    "formatted_name": output_file_name
                })
        else:
            output_file_name = f"{dir_name}.{output_format}"
            output_rel_path = os.path.join(parent_dir, output_file_name).replace(os.sep, "/")
            output_files.append({
                "name": dir_name,
                "url": output_rel_path,
                "formatted_name": output_file_name
            })

        current_node["__dirs__"].append({
            "name": dir_name,
            "files": output_files
        })

    # 生成不同格式的索引文件
    try:
        if output_format == 'html':
            index_content = index_template.render(
                repo_name=repo_name,
                file_tree=file_tree,
                max_depth=MAX_DIRECTORY_DEPTH
            )
            index_filename = "index.html"
        else:  # markdown
            # 生成Markdown格式目录
            index_content = f"# {repo_name} 源代码目录\n\n"
            index_content += f"> 说明: 深度为{MAX_DIRECTORY_DEPTH}的目录已合并其所有内容（包括子目录）\n\n"

            def add_directory_to_md(node, path="", level=1):
                content = ""
                # 添加子目录
                for dir_name, dir_node in node.items():
                    if dir_name in ("__files__", "__dirs__"):
                        continue
                    new_path = f"{path}/{dir_name}" if path else dir_name
                    content += f"{'#' * (level + 1)} {dir_name}\n\n"
                    content += add_directory_to_md(dir_node, new_path, level + 1)

                # 添加目标目录（已合并）
                if "__dirs__" in node and node["__dirs__"]:
                    content += f"{'#' * (level + 1)} 合并的目标目录\n\n"
                    for dir_item in node["__dirs__"]:
                        content += f"- {dir_item['name']}:\n"
                        for file in dir_item['files']:
                            content += f"  - [{file['name']}]({file['url']})\n"
                    content += "\n"

                # 添加文件
                if "__files__" in node and node["__files__"]:
                    content += f"{'#' * (level + 1)} 文件列表\n\n"
                    for file in node["__files__"]:
                        content += f"- [{file['name']}]({file['url']})\n"
                    content += "\n"
                return content

            index_content += add_directory_to_md(file_tree)
            index_filename = "index.md"

        index_path = os.path.join(output_dir, index_filename)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_content)
        click.echo(f"🏠 生成目录索引: {index_path}")
    except Exception as e:
        click.echo(f"❌ 生成目录索引失败: {str(e)}")


@click.command()
@click.option("--local-repo", required=True, help="本地仓库路径（例如: ../qemu）")
@click.option("--output-dir", "-o", default=DEFAULT_OUTPUT_DIR, help="输出目录，默认 ./output")
@click.option("--output-format", "-f", type=click.Choice(['md','html']), default='md',
              help="输出文件格式，选项: html, md, 默认 md")
@click.option("--ignore", "-i", multiple=True, help="额外的忽略模式（可多次指定）")
@click.option("--highlight-theme", default=HIGHLIGHT_THEME, help="代码高亮主题")
@click.option("--batch-size", default=BATCH_SIZE, help="每批处理的文件数量，默认 200")
@click.option("--max-workers", default=MAX_WORKERS, type=int,
              help=f"最大工作线程数，默认自动调整（当前系统推荐 {get_optimal_workers()}）")
@click.option("--max-file-size", default=MAX_FILE_SIZE//1024//1024, help="跳过的最大文件大小（MB），默认 10")
@click.option("--max-depth", default=MAX_DIRECTORY_DEPTH, type=int,
              help=f"最内层文件夹的深度，此深度的文件夹及其所有内容将被合并，默认 {MAX_DIRECTORY_DEPTH}")
def main(local_repo, output_dir, output_format, ignore, highlight_theme, batch_size, max_workers, max_file_size, max_depth):
    """多线程批量处理源代码转换工具，支持指定最内层目录深度并合并其所有内容"""
    # 更新全局配置
    global HIGHLIGHT_THEME, IGNORE_PATTERNS, BATCH_SIZE, MAX_FILE_SIZE, MAX_WORKERS, MAX_DIRECTORY_DEPTH
    HIGHLIGHT_THEME = highlight_theme
    IGNORE_PATTERNS += list(ignore)
    BATCH_SIZE = batch_size
    MAX_FILE_SIZE = max_file_size * 1024 * 1024  # 转换为字节
    MAX_WORKERS = max_workers if max_workers is not None else get_optimal_workers()
    MAX_DIRECTORY_DEPTH = max_depth
    click.echo(
        f"🔧 配置: 每批处理{batch_size}个文件，使用{MAX_WORKERS}个线程，"
        f"最内层目录深度{MAX_DIRECTORY_DEPTH}，合并文件大小限制{MERGED_FILE_SIZE_LIMIT/1024/1024:.1f}MiB"
    )

    # 验证仓库路径
    try:
        repo_path, repo_name = validate_local_repo(local_repo)
        click.echo(f"✅ 仓库验证通过: {repo_path}")
    except Exception as e:
        click.error(f"❌ 仓库验证失败: {e}")
        return

    # 准备输出目录
    if os.path.exists(output_dir):
        click.echo(f"🧹 清理旧输出目录: {output_dir}")
        shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)

    # 收集所有需要处理的文件和目标目录
    click.echo("\n🔍 扫描仓库文件和目录...")
    non_target_files, target_dirs, target_dir_set = collect_files_and_dirs_to_process(repo_path)
    if not non_target_files and not target_dirs:
        click.echo("ℹ️ 没有可处理的文件或目录")
        return

    # 多线程处理文件和目录
    click.echo(
        f"\n📄 开始处理文件和目录（批大小{batch_size}，{MAX_WORKERS}个并行线程，输出{output_format}格式）...")
    formatter = get_safe_formatter(highlight_theme, output_format)
    processed_count = 0
    total_items = len(non_target_files) + len(target_dirs)

    # 先处理目标目录
    if target_dirs:
        click.echo(f"\n📂 开始处理{len(target_dirs)}个目标深度目录...")
        dir_process_func = partial(
            process_target_directory,
            repo_root=repo_path,
            output_dir=output_dir,
            formatter=formatter,
            output_format=output_format
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(dir_process_func, dir_path)
                       for dir_path in target_dirs.keys()]

            for future in concurrent.futures.as_completed(futures):
                success, message, _ = future.result()
                click.echo(message)
                if success:
                    processed_count += 1

        gc.collect()
        progress = (processed_count / total_items) * 100
        click.echo(f"📊 进度: {processed_count}/{total_items} ({progress:.1f}%)")

    # 然后处理常规文件
    if non_target_files:
        click.echo(f"\n📄 开始处理{len(non_target_files)}个常规文件...")
        file_process_func = partial(
            process_single_file,
            repo_root=repo_path,
            output_dir=output_dir,
            formatter=formatter,
            output_format=output_format
        )

        for i in range(0, len(non_target_files), batch_size):
            # 检查内存使用情况
            wait_for_memory()

            # 获取当前批次文件
            batch_files = non_target_files[i:i+batch_size]
            batch_num = i // batch_size + 1
            click.echo(f"\n📦 处理批次{batch_num}（共{len(batch_files)}个文件）")

            # 使用线程池并行处理当前批次
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                results = executor.map(file_process_func, batch_files, chunksize=min(50, batch_size//MAX_WORKERS))
                
                for success, message in results:
                    click.echo(message)
                    if success:
                        processed_count += 1

            # 强制垃圾回收
            gc.collect()
            progress = (processed_count / total_items) * 100
            click.echo(f"📊 进度: {processed_count}/{total_items} ({progress:.1f}%)")

    # 生成目录索引
    click.echo("\n📋 生成目录索引...")
    generate_index(repo_path, output_dir, repo_name,
                   non_target_files, target_dirs, target_dir_set, output_format)

    # 完成消息
    click.echo(f"\n🎉 所有处理完成!")
    click.echo(f"📌 输出目录: {os.path.abspath(output_dir)}")
    index_filename = "index.html" if output_format == 'html' else "index.md"
    click.echo(
        f"🌐 索引文件: file://{os.path.abspath(os.path.join(output_dir, index_filename))}")


# 初始化Jinja2模板环境（仅HTML格式需要）
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    auto_reload=False,
    cache_size=0  # 禁用模板缓存
)
file_template = env.get_template("file.html") if os.path.exists(
    os.path.join(TEMPLATE_DIR, "file.html")) else None
index_template = env.get_template("index.html") if os.path.exists(
    os.path.join(TEMPLATE_DIR, "index.html")) else None

if __name__ == "__main__":
    main()