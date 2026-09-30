"""极简表单解析：urlencoded 与 multipart/form-data（照片上传），仅用于本演示。"""


def parse_form(handler):
    ctype = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b""
    if ctype.startswith("multipart/form-data"):
        boundary = ctype.split("boundary=", 1)[1].strip().encode()
        return _parse_multipart(raw, boundary)
    from urllib.parse import parse_qs
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}, {}


def _parse_multipart(raw, boundary):
    fields, files = {}, {}
    delim = b"--" + boundary
    for part in raw.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, body = part.split(b"\r\n\r\n", 1)
        # 只解析第一行（Content-Disposition），避免把 Content-Type 拼进 filename
        first_line = header_blob.decode("utf-8", "replace").split("\r\n", 1)[0]
        name = None
        filename = None
        for token in first_line.split(";"):
            token = token.strip()
            if token.startswith("name="):
                name = token[5:].strip('"')
            elif token.startswith("filename="):
                filename = token[9:].strip('"')
        if name is None:
            continue
        body = body.rstrip(b"\r\n")
        if filename is not None:
            if filename and body:
                files[name] = (filename, body)
        else:
            fields[name] = body.decode("utf-8", "replace")
    return fields, files
