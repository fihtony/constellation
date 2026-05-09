# Office Agent — Decision Policy

## Before Processing Any File

1. Confirm the file path is within the **Target Files / Directories** provided in the task.
2. Check the file type; use `run_local_command` (`file <path>`) to detect binary vs text.
3. If the path escapes the authorized target root: report as permission error and skip.
4. If the file is binary and unsupported: note it in warnings, do not attempt to read raw bytes.

## Capability-Driven Action

1. If capability is `office.document.summarize` or similar → read files and produce a `summary.md`. No source file modifications.
2. If capability is `office.data.analyze` → read data files and produce `analysis.md`. No source file modifications.
3. If capability is `office.folder.organize` → inventory files, write a plan to `organization-plan.json`, then execute if output mode is INPLACE.
4. For any other capability: read the task prompt carefully and use best judgment to complete it.

## Large File Handling

1. For files > 50 MB: warn in progress, process using `run_local_command` (`head`, `wc -l`) rather than full read.
2. For files > 200 MB: skip and note in warnings as oversized.

## Summary Quality

1. Summaries must be factual and grounded in the file content.
2. Do not invent or extrapolate beyond what is in the files.
3. Include: file/directory name, key topics, main findings, word count or row count.
