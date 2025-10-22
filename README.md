# src2ima

A multi-threaded batch processing tool for converting source code files to Markdown (md) or HTML format, with support for limiting maximum directory depth.


## Features

- **Batch Conversion**: Process multiple source code files in parallel using multi-threading
- **Format Support**: Convert to Markdown (`md`) or HTML (`html`)
- **Directory Control**: Limit maximum directory depth (auto-flattening for deeper directories)
- **Customizable Ignorance**: Skip specific files/directories with ignore patterns
- **Code Highlighting**: Support for custom syntax highlighting themes
- **Resource Management**: Configurable batch size and worker threads for optimal performance


## Requirements

- Python 3.12+
- Required dependencies:
  ```
    click>=8.1.7
    jupyterlab-pygments>=0.1.2
    markdown2>= 2.5.4
    psutil>=5.9.0
  ```

Install dependencies with:
```bash
pip install -r requirements.txt
```


## Usage

### Basic Command

```bash
python src2ima.py --local-repo /path/to/your/repo
```

### Full Options

```
Usage: src2ima.py [OPTIONS]

  Multi-threaded batch processing source code conversion tool, supports limiting maximum directory depth

Options:
  --local-repo TEXT              Local repository path (e.g., ../qemu) [required]
  -o, --output-dir TEXT          Output directory, default ./output
  -f, --output-format [md|html]  Output file format, options: html, md, default md
  -i, --ignore TEXT              Additional ignore patterns (can be specified multiple times)
  --highlight-theme TEXT         Code highlighting theme
  --batch-size INTEGER           Number of files to process per batch, default 100
  --max-workers INTEGER          Maximum worker threads, default automatically determined by CPU cores
  --max-file-size INTEGER        Maximum file size to skip (MB), default 10
  --max-depth INTEGER            Maximum directory depth, beyond which will be flattened, default 5
  --single-file                  Merge all code into a single Markdown file
  --force                        Force delete output directory without confirmation
  --help                         Show this message and exit.
```


### Examples

1. Convert a repository to HTML format:
   ```bash
   python src2ima.py --local-repo ../my-project -f html -o ./html-output
   ```

2. Convert with custom ignore patterns and larger batch size:
   ```bash
   python src2ima.py --local-repo ../my-code -i "*.log" -i "temp/" --batch-size 200
   ```

3. Limit directory depth to 3 levels and skip files larger than 5MB:
   ```bash
   python src2ima.py --local-repo ../my-repo --max-depth 3 --max-file-size 5
   ```

4. Merge all code into a single **Markdown** file:
   ```bash
   python src2ima.py --local-repo ../my-project --single-file -o ./output
   ```
   This will generate a file named `<repo_name>_all.md` containing all source code files merged together.

5. Force delete output directory without confirmation (useful for automation):
   ```bash
   python src2ima.py --local-repo ../my-project --force
   ```
   This will skip the deletion confirmation prompt and directly clean the output directory if it exists.


## Notes

- The tool automatically skips binary files and common version control directories (e.g., `.git/`, `node_modules/`)
- Overly long file paths will be automatically shortened to avoid system limitations
- An index file (`index.md` or `index.html`) will be generated in the output directory for easy navigation
- Memory usage is monitored during processing to prevent excessive resource consumption
- If you use the option to designate the output directory, this path will be removed first! Be careful!