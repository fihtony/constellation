# Office Agent — Failure Handling

## Unauthorized Path Access

- Reject immediately with `fail_current_task`.
- Include: requested path, authorization failure reason.
- Never reveal the contents of files outside authorized paths.

## Unsupported File Type

- Report the file extension that is not supported.
- Include the list of supported extensions in the error.

## Corrupt or Unreadable File

- Report the file name and the parsing error.
- Do not attempt to continue with partial data from a corrupt file.

## File Too Large

- Report the file size and the configured size limit.
- Suggest that the user split the file or increase the limit if appropriate.

## Processing Errors

- For PDF extraction errors: retry once, then fail.
- For Excel parsing errors: try alternate library (xlrd → openpyxl), then fail if both fail.
- All failures must use `fail_current_task` with structured error details.
