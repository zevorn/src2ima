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

# Configuration items
DEFAULT_OUTPUT_DIR = "./output"
IGNORE_PATTERNS = [
    # Directories (ending with / indicates directory)
    ".git/", ".github/", "node_modules/", "__pycache__/",
    ".vscode/", ".cache/", "build/", ".sdk/", "dist/", "bin/",
    "pc-bios/", "rust/target/",
    # File extensions (starting with .)
    ".bin", ".rom", ".bz2", ".gz", ".zip", ".tar", ".7z",
    ".out", ".o", ".so", ".dll", ".pyc", ".pyo", ".patch",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico",
    ".pdf", ".docx", ".xlsx", ".pptx",
    # Specific filenames
    ".gdb_history", ".clang-format", ".git-submodule-status",
    "Makefile", "makefile", "README", "LICENSE"
]
HIGHLIGHT_THEME = "github-dark"  # Dark theme
TEMPLATE_DIR = "./templates"
MAX_PATH_LENGTH = 255
BATCH_SIZE = 1000  # Number of files to process per batch
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB, files larger than this will be skipped
MEMORY_THRESHOLD = 0.8  # Memory usage threshold (80%)
MAX_WORKERS = None  # Maximum number of worker threads
MAX_DIRECTORY_DEPTH = 5  # Maximum directory depth, beyond which will be flattened
TEXT_CHARS = bytes([7, 8, 9, 10, 12, 13, 27]) + \
    bytes(range(0x20, 0x100))  # Text file characteristic bytes


def limit_directory_depth(rel_path, max_depth=MAX_DIRECTORY_DEPTH):
    """
    Limit directory depth, directories beyond max_depth will be flattened to max_depth level
    Example: a/b/c/d/e/f/g/h/i/j/file.txt becomes a/b/c/d/e/f/g/h/i_j_file.txt
    """
    # Split path into parts
    path_parts = rel_path.split(os.sep)
    # Filter empty strings (handle possible empty path parts)
    path_parts = [part for part in path_parts if part]

    # If path depth does not exceed maximum limit, return original path
    if len(path_parts) <= max_depth:
        return rel_path

    # Keep first max_depth level directories
    dir_parts = path_parts[:max_depth]
    # Remaining parts (including filename) will be merged into new filename
    remaining_parts = path_parts[max_depth:]

    # Merge remaining parts as new filename, using special separator to avoid conflicts
    new_filename = "_".join(remaining_parts)

    # Combine new path
    return os.path.join(os.sep.join(dir_parts), new_filename)


def is_binary_file(file_path, sample_size=1024):
    """Determine if file is binary"""
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
    """Get current process memory usage (percentage)"""
    process = psutil.Process(os.getpid())
    return process.memory_percent()


def wait_for_memory():
    """Wait for memory usage to drop below threshold"""
    while get_memory_usage() > MEMORY_THRESHOLD * 100:
        current_usage = get_memory_usage()
        click.echo(f"⚠️  Memory usage too high ({current_usage:.1f}%), waiting for release...")
        gc.collect()  # Force garbage collection
        psutil.sleep(2)


def should_ignore(path):
    """Determine if path should be ignored"""
    path_lower = path.lower()
    # Check file extensions
    for pattern in IGNORE_PATTERNS:
        if pattern.startswith('.') and len(pattern) > 1:
            if path_lower.endswith(pattern):
                return True
    # Check directories
    for pattern in IGNORE_PATTERNS:
        if pattern.endswith('/'):
            dir_name = pattern.rstrip('/')
            if dir_name in path.split(os.sep):
                return True
    # Check filenames
    for pattern in IGNORE_PATTERNS:
        if not pattern.startswith('.') and not pattern.endswith('/'):
            if os.path.basename(path) == pattern:
                return True
    # Check if file is binary
    if os.path.isfile(path) and is_binary_file(path):
        return True
    return False


def get_safe_formatter(theme, output_format):
    """Get safe code formatter based on output format, with caching mechanism"""
    cache_key = f"{theme}_{output_format}"
    if not hasattr(get_safe_formatter, "_cache"):
        get_safe_formatter._cache = {}

    if cache_key not in get_safe_formatter._cache:
        try:
            formatter = HtmlFormatter(style=theme, linenos=True)
        except ClassNotFound:
            click.warning(f"⚠️  Highlight theme '{theme}' does not exist, using default theme")
            formatter = HtmlFormatter(style="default", linenos=True)
        get_safe_formatter._cache[cache_key] = formatter

    return get_safe_formatter._cache[cache_key]


def validate_local_repo(local_path):
    """Validate local repository path validity"""
    local_path = os.path.abspath(local_path)
    if not os.path.exists(local_path):
        raise ValueError(f"Path does not exist: {local_path}")
    if not os.path.isdir(local_path):
        raise ValueError(f"Not a directory: {local_path}")
    return local_path, os.path.basename(local_path)


def shorten_long_path(original_path, max_length, output_format):
    """Shorten overly long file paths, preserve original extension and add new format suffix"""
    if len(original_path) <= max_length:
        return original_path

    dir_name = os.path.dirname(original_path)
    file_name = os.path.basename(original_path)

    # Preserve original filename and extension, only add format suffix at the end
    hash_suffix = hashlib.md5(original_path.encode()).hexdigest()[:6]
    base_name = os.path.splitext(file_name)[0]
    original_ext = os.path.splitext(file_name)[1]
    new_file_name = f"{base_name}_{hash_suffix}{original_ext}.{output_format}"

    return os.path.join(dir_name, new_file_name)


def process_single_file_to_content(file_path, repo_root, formatter, output_format):
    """Process single file and return content string (for single file mode)"""
    try:
        # Skip oversized files
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            rel_path = os.path.relpath(file_path, repo_root)
            return (False, f"⏭️  Skip oversized file ({file_size//1024//1024}MB): {rel_path}", None)

        # Get relative path
        rel_path = os.path.relpath(file_path, repo_root)

        # Check if should be ignored
        if should_ignore(rel_path) or should_ignore(file_path):
            if is_binary_file(file_path):
                return (False, f"🔇 Ignore binary file: {rel_path}", None)
            else:
                return (False, f"🔇 Ignore file: {rel_path}", None)

        # Read file content
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()

        # Process content - only support Markdown format
        ext = os.path.splitext(file_path)[1].lower()
        file_ext = ext.lstrip('.') if ext else 'txt'

        if ext == ".md":
            final_content = f"## {rel_path}\n\n{content}\n\n---\n\n"
        else:
            # Generate Markdown code block
            try:
                lexer = get_lexer_for_filename(file_path)
                lexer_name = lexer.aliases[0] if lexer.aliases else lexer.name.lower()
            except Exception:
                try:
                    lexer = get_lexer_by_name(file_ext)
                    lexer_name = file_ext
                except Exception:
                    lexer_name = 'text'

            # Escape backticks in code
            escaped_content = content.replace('`', '\\`')
            final_content = f"## {rel_path}\n\n```{lexer_name}\n{escaped_content}\n```\n\n---\n\n"

        return (True, f"🟢 Processed: {rel_path}", final_content)

    except Exception as e:
        rel_path = os.path.relpath(file_path, repo_root)
        return (False, f"❌ Processing failed {rel_path}: {str(e)[:50]}", None)


def process_single_file(file_path, repo_root, output_dir, formatter, output_format):
    """Process single file and convert to specified format, preserve original extension and limit directory depth"""
    try:
        # Skip oversized files
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            rel_path = os.path.relpath(file_path, repo_root)
            return (False, f"⏭️  Skip oversized file ({file_size//1024//1024}MB): {rel_path}")

        # Get relative path and limit directory depth
        rel_path = os.path.relpath(file_path, repo_root)
        limited_rel_path = limit_directory_depth(rel_path)

        # Check if should be ignored
        if should_ignore(rel_path) or should_ignore(file_path):
            if is_binary_file(file_path):
                return (False, f"🔇 Ignore binary file: {rel_path}")
            else:
                return (False, f"🔇 Ignore file: {rel_path}")

        # Generate output file path - preserve original extension and add new format suffix
        file_name = os.path.basename(limited_rel_path)
        new_file_name = f"{file_name}.{output_format}"
        output_rel_path = os.path.join(
            os.path.dirname(limited_rel_path), new_file_name)
        output_path = os.path.join(output_dir, output_rel_path)
        output_path = shorten_long_path(
            output_path, MAX_PATH_LENGTH, output_format)

        # Create output directory
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Read file content
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()

        # Process content based on file type and output format
        ext = os.path.splitext(file_path)[1].lower()
        file_ext = ext.lstrip('.') if ext else 'txt'

        if output_format == 'html':
            if ext == ".md":
                # Convert Markdown file to HTML
                html_content = markdown2.markdown(
                    content,
                    extras=["fenced-code-blocks", "tables"]
                )
                highlight_css = ""
            else:
                # Convert code file to highlighted HTML
                try:
                    lexer = get_lexer_for_filename(file_path)
                except Exception:
                    lexer = TextLexer()
                html_content = highlight(content, lexer, formatter)
                highlight_css = formatter.get_style_defs(".highlight")

            # Generate complete HTML using template
            final_content = file_template.render(
                title=rel_path,
                rel_path=rel_path,
                content=html_content,
                highlight_css=highlight_css
            )

        else:  # output_format == 'md'
            if ext == ".md":
                # Markdown file preserves original extension, add .md suffix
                final_content = f"## {rel_path}\n\n{content}"
            else:
                # Manually generate Markdown code blocks
                try:
                    lexer = get_lexer_for_filename(file_path)
                    lexer_name = lexer.aliases[0] if lexer.aliases else lexer.name.lower(
                    )
                except Exception:
                    try:
                        lexer = get_lexer_by_name(file_ext)
                        lexer_name = file_ext
                    except Exception:
                        lexer_name = 'text'

                # Escape backticks in code
                escaped_content = content.replace('`', '\\`')
                final_content = f"## {rel_path}\n\n``` {lexer_name}\n{escaped_content}\n```\n"

        # Write output file
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        # If path was shortened, show original path and processed path in log
        if rel_path != limited_rel_path:
            return (True, f"🟢 Converted to {output_format}: {rel_path} → {limited_rel_path} → {os.path.basename(output_path)}")
        else:
            return (True, f"🟢 Converted to {output_format}: {rel_path} → {os.path.basename(output_path)}")

    except Exception as e:
        rel_path = os.path.relpath(file_path, repo_root)
        return (False, f"❌ Processing failed {rel_path}: {str(e)[:50]}")


def collect_files_to_process(repo_path):
    """Collect all file paths that need to be processed"""
    files_to_process = []
    stack = [repo_path]

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not should_ignore(entry.path):
                                stack.append(entry.path)
                        else:
                            if not should_ignore(entry.path):
                                files_to_process.append(entry.path)
                    except Exception as e:
                        click.echo(f"⚠️  Error accessing {entry.path}: {str(e)[:30]}")
                        continue
        except PermissionError:
            click.echo(f"🚫 No permission to access directory: {current_dir}")
            continue
        except Exception as e:
            click.echo(f"⚠️  Error processing directory {current_dir}: {str(e)[:30]}")
            continue

    click.echo(f"📊 Total {len(files_to_process)} processable files found")
    return files_to_process


def generate_index(repo_root, output_dir, repo_name, files_to_process, output_format):
    """Generate directory index page, generate different types of indexes based on output format"""
    file_tree = {"__files__": []}  # Root node contains __files__ when initialized

    for file_path in files_to_process:
        rel_path = os.path.relpath(file_path, repo_root)
        limited_rel_path = limit_directory_depth(rel_path)
        rel_dir = os.path.dirname(limited_rel_path)
        file_name = os.path.basename(rel_path)
        limited_file_name = os.path.basename(limited_rel_path)

        # Build directory tree structure (based on processed path)
        current_node = file_tree
        dir_parts = rel_dir.split(os.sep) if rel_dir != '.' else []

        for part in dir_parts:
            if not part:
                continue
            if part not in current_node:
                current_node[part] = {"__files__": []}
            current_node = current_node[part]
            if "__files__" not in current_node:
                current_node["__files__"] = []

        # Add file information - preserve original extension and add new format suffix
        output_file_name = f"{limited_file_name}.{output_format}"
        output_rel_path = os.path.join(
            rel_dir, output_file_name).replace(os.sep, "/")
        # Keep original filename in index for easy identification
        current_node["__files__"].append({
            "name": file_name,
            "limited_name": limited_file_name,
            "url": output_rel_path,
            "formatted_name": output_file_name
        })

    # Generate different index files based on output format
    try:
        if output_format == 'html':
            index_content = index_template.render(
                repo_name=repo_name,
                file_tree=file_tree,
                max_depth=MAX_DIRECTORY_DEPTH
            )
            index_filename = "index.html"
        else:  # markdown
            # Generate Markdown format directory
            index_content = f"# {repo_name} Source Code Directory\n\n"
            index_content += f"> Note: Directories beyond {MAX_DIRECTORY_DEPTH} levels have been flattened\n\n"

            def add_directory_to_md(node, path="", level=1):
                content = ""
                # Add directories
                for dir_name, dir_node in node.items():
                    if dir_name == "__files__":
                        continue
                    new_path = f"{path}/{dir_name}" if path else dir_name
                    content += f"{'#' * (level + 1)} {dir_name}\n\n"
                    content += add_directory_to_md(dir_node,
                                                   new_path, level + 1)

                # Add files
                if "__files__" in node and node["__files__"]:
                    content += f"{'#' * (level + 1)} File List\n\n"
                    for file in node["__files__"]:
                        # If filename was modified, show original filename and processed filename
                        if file['name'] != file['limited_name']:
                            content += f"- [{file['name']} ({file['limited_name']})]({file['url']})\n"
                        else:
                            content += f"- [{file['name']}]({file['url']})\n"
                    content += "\n"
                return content

            index_content += add_directory_to_md(file_tree)
            index_filename = "index.md"

        index_path = os.path.join(output_dir, index_filename)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_content)
        click.echo(f"🏠 Generated directory index: {index_path}")
    except Exception as e:
        click.echo(f"❌ Failed to generate directory index: {str(e)}")


@click.command()
@click.option("--local-repo", required=True, help="Local repository path (e.g., ../qemu)")
@click.option("--output-dir", "-o", default=DEFAULT_OUTPUT_DIR, help="Output directory, default ./output")
@click.option("--output-format", "-f", type=click.Choice(['md','html']), default='md',
              help="Output file format, options: html, md, default md")
@click.option("--ignore", "-i", multiple=True, help="Additional ignore patterns (can be specified multiple times)")
@click.option("--highlight-theme", default=HIGHLIGHT_THEME, help="Code highlighting theme")
@click.option("--batch-size", default=BATCH_SIZE, help="Number of files to process per batch, default 100")
@click.option("--max-workers", default=MAX_WORKERS, type=int,
              help=f"Maximum worker threads, default automatically determined by CPU cores (current system recommends {os.cpu_count() * 2})")
@click.option("--max-file-size", default=MAX_FILE_SIZE//1024//1024, help="Maximum file size to skip (MB), default 10")
@click.option("--max-depth", default=MAX_DIRECTORY_DEPTH, type=int,
              help=f"Maximum directory depth, beyond which will be flattened, default {MAX_DIRECTORY_DEPTH}")
@click.option("--single-file", is_flag=True, help="Merge all code into a single Markdown file")
@click.option("--force", "-f", is_flag=True, help="Force delete output directory without confirmation")
def main(local_repo, output_dir, output_format, ignore, highlight_theme, batch_size, max_workers, max_file_size, max_depth, single_file, force):
    """Multi-threaded batch processing source code conversion tool, supports limiting maximum directory depth"""
    # Global configuration update
    global HIGHLIGHT_THEME, IGNORE_PATTERNS, BATCH_SIZE, MAX_FILE_SIZE, MAX_WORKERS, MAX_DIRECTORY_DEPTH
    HIGHLIGHT_THEME = highlight_theme
    IGNORE_PATTERNS += list(ignore)
    BATCH_SIZE = batch_size
    MAX_FILE_SIZE = max_file_size * 1024 * 1024  # Convert to bytes
    MAX_WORKERS = max_workers if max_workers is not None else os.cpu_count() * \
        2  # Default CPU cores * 2
    MAX_DIRECTORY_DEPTH = max_depth

    # 单文件模式下强制使用 Markdown 格式
    if single_file and output_format != 'md':
        click.echo("⚠️  Single file mode only supports Markdown format, automatically switching to md")
        output_format = 'md'

    click.echo(
        f"🔧 Configuration: Process {batch_size} files per batch, using {MAX_WORKERS} worker threads, maximum directory depth {MAX_DIRECTORY_DEPTH}")
    if single_file:
        click.echo("📄 Mode: Merge all code into a single Markdown file")

    # Validate repository path
    try:
        repo_path, repo_name = validate_local_repo(local_repo)
        click.echo(f"✅ Repository validated: {repo_path}")
    except Exception as e:
        click.echo(f"❌ Repository validation failed: {e}")
        return

    # Prepare output directory
    if os.path.exists(output_dir):
        if force:
            click.echo(f"🧹 Force cleaning old output directory: {output_dir}")
            shutil.rmtree(output_dir, ignore_errors=True)
        elif click.confirm(f"⚠️  Output directory already exists: {output_dir}\nDo you want to delete it and continue?"):
            click.echo(f"🧹 Cleaning old output directory: {output_dir}")
            shutil.rmtree(output_dir, ignore_errors=True)
        else:
            click.echo("❌ Operation cancelled by user")
            return
    os.makedirs(output_dir, exist_ok=True)

    # Collect all files to process
    click.echo("\n🔍 Scanning repository files...")
    files_to_process = collect_files_to_process(repo_path)
    if not files_to_process:
        click.echo("ℹ️  No processable files found")
        return

    # Batch process files (multi-threaded)
    formatter = get_safe_formatter(highlight_theme, output_format)
    processed_count = 0
    total_files = len(files_to_process)

    if single_file:
        # 单文件模式：收集所有内容到一个文件
        click.echo(f"\n📄 Starting file processing (single file mode, {MAX_WORKERS} threads parallel)...")
        all_contents = []

        # Create partial function for single file mode
        process_func = partial(
            process_single_file_to_content,
            repo_root=repo_path,
            formatter=formatter,
            output_format='md'
        )

        for i in range(0, total_files, batch_size):
            wait_for_memory()
            batch_files = files_to_process[i:i+batch_size]
            batch_num = i // batch_size + 1
            click.echo(f"\n📦 Processing batch {batch_num} ({len(batch_files)} files total)")

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(process_func, file_path) for file_path in batch_files]

                for future in concurrent.futures.as_completed(futures):
                    success, message, content = future.result()
                    click.echo(message)
                    if success and content:
                        all_contents.append(content)
                        processed_count += 1

            gc.collect()
            progress = (processed_count / total_files) * 100
            click.echo(f"📊 Progress: {processed_count}/{total_files} ({progress:.1f}%)")

        # Write all content to single file
        output_file = os.path.join(output_dir, f"{repo_name}_all.md")
        click.echo(f"\n💾 Writing to single file: {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# {repo_name} - Complete Source Code\n\n")
            f.write(f"> Generated: {total_files} files, {processed_count} successfully processed\n\n")
            f.write("---\n\n")
            f.writelines(all_contents)

        click.echo(f"✅ Single file generated: {output_file}")

    else:
        # 原有的多文件模式
        click.echo(
            f"\n📄 Starting file processing ({batch_size} files per batch, {MAX_WORKERS} threads parallel, output {output_format} format)...")

        # Create partial function, fix common parameters
        process_func = partial(
            process_single_file,
            repo_root=repo_path,
            output_dir=output_dir,
            formatter=formatter,
            output_format=output_format
        )

        for i in range(0, total_files, batch_size):
            # Check memory usage
            wait_for_memory()

            # Get current batch files
            batch_files = files_to_process[i:i+batch_size]
            batch_num = i // batch_size + 1
            click.echo(f"\n📦 Processing batch {batch_num} ({len(batch_files)} files total)")

            # Use thread pool to process current batch files in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                # Submit all tasks
                futures = [executor.submit(process_func, file_path)
                           for file_path in batch_files]

                # Process results
                for future in concurrent.futures.as_completed(futures):
                    success, message = future.result()
                    click.echo(message)
                    if success:
                        processed_count += 1

            # Force garbage collection
            gc.collect()
            progress = (processed_count / total_files) * 100
            click.echo(f"📊 Progress: {processed_count}/{total_files} ({progress:.1f}%)")

        # Generate directory index
        click.echo("\n📋 Generating directory index...")
        generate_index(repo_path, output_dir, repo_name,
                       files_to_process, output_format)

    # Completion message
    click.echo(f"\n🎉 All processing completed!")
    click.echo(f"📌 Output directory: {os.path.abspath(output_dir)}")

    if single_file:
        output_file = os.path.join(output_dir, f"{repo_name}_all.md")
        click.echo(f"📄 Output file: file://{os.path.abspath(output_file)}")
    else:
        index_filename = "index.html" if output_format == 'html' else "index.md"
        click.echo(
            f"🌐 Index file: file://{os.path.abspath(os.path.join(output_dir, index_filename))}")


# Initialize Jinja2 template environment (only needed for HTML format)
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    auto_reload=False,
    cache_size=0  # Disable template caching
)
file_template = env.get_template("file.html") if os.path.exists(
    os.path.join(TEMPLATE_DIR, "file.html")) else None
index_template = env.get_template("index.html") if os.path.exists(
    os.path.join(TEMPLATE_DIR, "index.html")) else None

if __name__ == "__main__":
    main()