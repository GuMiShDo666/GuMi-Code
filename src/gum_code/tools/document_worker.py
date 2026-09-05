"""Bounded, one-shot local decoders. No credentials, client, or Office runtime."""

import io
import json
import math
import os
import posixpath
import stat
import sys
import warnings
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import TreeBuilder

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_PAGE_CHARS = 200_000
MAX_PDF_BATCH = 5
MAX_DOCX_BATCH = 100
MAX_ZIP_ENTRIES = 2000
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_XML_DEPTH = 64
MAX_XML_NODES = 100_000
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_EDGE = 10_000
MAX_RENDER_EDGE = 2048
MAX_RENDER_PIXELS = 4_000_000
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_WORKER_RESULT_CHARS = 17_000
MAX_WORKER_RESULT_BYTES = MAX_WORKER_RESULT_CHARS * 4
MAX_VISUALS_PER_RESULT = 100

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
V = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XI = "http://www.w3.org/2001/XInclude"
NS = {"w": W, "r": R, "a": A}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
VML_VECTOR_TAGS = {f"{{{V}}}{name}" for name in
                   ("group", "line", "polyline", "curve", "arc", "rect", "roundrect", "oval")}


class DecodeError(ValueError):
    """Only fixed reason codes cross the worker boundary."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dimensions(width: int, height: int) -> None:
    if (width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_EDGE
            or width * height > MAX_IMAGE_PIXELS):
        raise DecodeError("image_limit")


def _image(data: bytes):
    from PIL import Image

    if len(data) > MAX_IMAGE_BYTES:
        raise DecodeError("image_limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"]) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP"}:
                raise DecodeError("unsupported_media")
            _dimensions(*probe.size)
            if getattr(probe, "is_animated", False) or getattr(probe, "n_frames", 1) != 1:
                raise DecodeError("animated_image")
            probe.verify()
        image = Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"])
        try:
            _dimensions(*image.size)
            image.load()
        except BaseException:
            image.close()
            raise
        return image


def _png(image, crop=None, *, orient: bool = True) -> bytes:
    from PIL import Image, ImageOps

    corrected = ImageOps.exif_transpose(image) if orient else image.copy()
    try:
        if crop is not None:
            w, h = corrected.size
            x0, y0, x1, y1 = crop
            box = (math.floor(x0 * w), math.floor(y0 * h),
                   math.ceil(x1 * w), math.ceil(y1 * h))
            cropped = corrected.crop(box)
            corrected.close()
            corrected = cropped
        width, height = corrected.size
        scale = min(1, MAX_RENDER_EDGE / max(width, height),
                    math.sqrt(MAX_RENDER_PIXELS / (width * height)))
        if scale < 1:
            corrected.thumbnail((max(1, int(width * scale)), max(1, int(height * scale))))
        rgba = corrected.convert("RGBA")
        try:
            clean = Image.new("RGB", rgba.size, "white")
            try:
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="PNG")
                encoded = output.getvalue()
            finally:
                clean.close()
        finally:
            rgba.close()
        if len(encoded) > MAX_PNG_BYTES:
            raise DecodeError("image_limit")
        return encoded
    finally:
        corrected.close()


class _BoundedTree(TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.nodes = self.characters = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > MAX_XML_DEPTH or self.nodes > MAX_XML_NODES:
            raise DecodeError("xml_limit")
        if tag.startswith("{" + XI + "}"):
            raise DecodeError("unsafe_xml")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)

    def data(self, data):
        self.characters += len(data)
        if self.characters > MAX_XML_BYTES:
            raise DecodeError("xml_limit")
        return super().data(data)


def _xml(data: bytes):
    from defusedxml.ElementTree import DefusedXMLParser

    parser = DefusedXMLParser(target=_BoundedTree(), forbid_dtd=True,
                             forbid_entities=True, forbid_external=True)
    for offset in range(0, len(data), 16_384):
        parser.feed(data[offset:offset + 16_384])
    return parser.close()


def _part_name(name: str) -> str:
    if (not name or "\\" in name or "\x00" in name or name.startswith("/")
            or urlsplit(name).scheme):
        raise DecodeError("unsafe_archive")
    normalized = posixpath.normpath(name)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise DecodeError("unsafe_archive")
    return normalized


class _Docx:
    """A single bounded archive; only requested, internal parts are read."""

    def __init__(self, data: bytes, version: str):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        self.version = version
        self.expanded = 0
        self.parts = {}
        self.cache = {}
        entries = self.archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise DecodeError("archive_limit")
        total = 0
        for item in entries:
            if ".." in item.filename.split("/"):
                raise DecodeError("unsafe_archive")
            name = _part_name(item.filename)
            mode = item.external_attr >> 16
            if (name in self.parts or item.flag_bits & 1 or stat.S_ISLNK(mode)
                    or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                raise DecodeError("unsafe_archive")
            total += item.file_size
            if total > MAX_EXPANDED_BYTES:
                raise DecodeError("archive_limit")
            self.parts[name] = item
        roots = self.relationships("")
        main = [rel for rel in roots.values() if rel[0].endswith("/officeDocument")]
        if len(main) != 1 or main[0][1] is None:
            raise DecodeError("invalid_document")
        self.main = main[0][1]
        types = _xml(self.read("[Content_Types].xml", MAX_XML_BYTES))
        self.defaults = {x.get("Extension", "").lower(): x.get("ContentType", "")
                         for x in types if x.tag.endswith("}Default")}
        self.overrides = {x.get("PartName", "").lstrip("/"): x.get("ContentType", "")
                          for x in types if x.tag.endswith("}Override")}
        root = _xml(self.read(self.main, MAX_XML_BYTES))
        body = root.find("w:body", NS)
        if body is None:
            raise DecodeError("unsupported_document")
        self.rels = self.relationships(self.main)
        self.blocks = [block for block in body if block.tag != f"{{{W}}}sectPr"]
        self.global_unparsed = []
        if any(t.endswith(("/header", "/footer", "/comments", "/footnotes", "/endnotes"))
               for t, _ in self.rels.values()):
            self.global_unparsed.append("non_body_content")

    def close(self):
        self.archive.close()

    def read(self, name: str, limit: int) -> bytes:
        if name in self.cache:
            return self.cache[name]
        item = self.parts.get(name)
        if item is None or item.is_dir():
            raise DecodeError("invalid_document")
        if item.file_size > limit:
            raise DecodeError("archive_limit")
        with self.archive.open(item) as stream:
            data = stream.read(limit + 1)
        self.expanded += len(data)
        if len(data) > limit or self.expanded > MAX_EXPANDED_BYTES:
            raise DecodeError("archive_limit")
        if name.endswith(".xml") or name.endswith(".rels"):
            self.cache[name] = data
        return data

    def relationships(self, part: str) -> dict:
        path = (posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
                if part else "_rels/.rels")
        if path not in self.parts:
            return {}
        root = _xml(self.read(path, MAX_XML_BYTES))
        result = {}
        for rel in root:
            identity = rel.get("Id", "")
            if not identity or identity in result:
                raise DecodeError("invalid_document")
            target = unquote(rel.get("Target", ""))
            location = urlsplit(target)
            external = (rel.get("TargetMode") == "External" or location.scheme
                        or location.netloc or location.query or location.fragment)
            resolved = None if external else _part_name(posixpath.join(posixpath.dirname(part), target))
            result[identity] = (rel.get("Type", ""), resolved)
        return result

    def _text(self, node) -> str:
        if node.tag in {f"{{{W}}}drawing", f"{{{W}}}pict", f"{{{W}}}del",
                        f"{{{W}}}ins", f"{{{W}}}txbxContent"}:
            return ""
        if node.tag == f"{{{W}}}t":
            return node.text or ""
        if node.tag == f"{{{W}}}tab":
            return "\t"
        if node.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
            return "\n"
        if node.tag == f"{{{W}}}tbl":
            rows = []
            for row in node.findall("w:tr", NS):
                cells = []
                for cell in row.findall("w:tc", NS):
                    span = cell.find("w:tcPr/w:gridSpan", NS)
                    merge = cell.find("w:tcPr/w:vMerge", NS)
                    attrs = []
                    if span is not None:
                        attrs.append("colspan=" + span.get(f"{{{W}}}val", "1"))
                    if merge is not None:
                        attrs.append("vmerge=" + merge.get(f"{{{W}}}val", "continue"))
                    cells.append("[cell" + (" " + ",".join(attrs) if attrs else "") + "]"
                                 + "".join(self._text(x) for x in cell) + "[/cell]")
                rows.append("[row]" + "".join(cells) + "[/row]")
            return "[table]\n" + "\n".join(rows) + "\n[/table]\n"
        text = "".join(self._text(x) for x in node)
        return text + ("\n" if node.tag == f"{{{W}}}p" else "")

    def _objects(self, node):
        if node.tag == f"{{{MC}}}AlternateContent":
            yield {"reason": "native_graphic", "readable": False}
            fallback = node.find(f"{{{MC}}}Fallback")
            if fallback is not None:
                for item in self._objects(fallback):
                    item["preview_only"] = True
                    yield item
            return
        if node.tag in {f"{{{W}}}drawing", f"{{{W}}}pict"}:
            refs = [x for x in node.iter() if x.tag in {f"{{{A}}}blip", f"{{{V}}}imagedata"}]
            native = any(x.tag.rsplit("}", 1)[0] not in {"{" + A, "{" + W, "{" + V}
                         and ("diagram" in x.tag or "wordprocessingShape" in x.tag
                              or "wordprocessingGroup" in x.tag) for x in node.iter())
            # A legacy bitmap wrapper is readable; its grouped/vector siblings are not rendered.
            native = native or any(
                x.tag in VML_VECTOR_TAGS
                or (x.tag == f"{{{V}}}shape" and (
                    not any(child.tag == f"{{{V}}}imagedata" for child in x.iter())
                    or x.get("type") not in {None, "#_x0000_t75"} or "path" in x.attrib
                )) for x in node.iter()
            )
            if not refs or native:
                yield {"reason": "native_graphic", "readable": False}
            transformed = any(x.tag in {f"{{{A}}}srcRect", f"{{{A}}}effectLst",
                                       f"{{{A}}}effectDag"}
                              or (x.tag == f"{{{A}}}xfrm" and any(k in x.attrib for k in ("rot", "flipH", "flipV")))
                              or (x.tag == f"{{{V}}}imagedata" and any(k in x.attrib for k in
                                  ("cropleft", "cropright", "croptop", "cropbottom", "gain", "blacklevel")))
                              or (x.tag.startswith("{" + V + "}") and (
                                  x.tag == f"{{{V}}}skew" or any(
                                      declaration.split(":", 1)[0].strip().lower() in {"rotation", "flip"}
                                      for declaration in x.get("style", "").split(";")
                                  )
                              ))
                              for x in node.iter())
            for ref in refs:
                identity = ref.get(f"{{{R}}}embed") or ref.get(f"{{{R}}}id")
                relation = self.rels.get(identity)
                if relation is None or relation[1] is None or ref.get(f"{{{R}}}link"):
                    yield {"reason": "external_or_missing_image", "readable": False}
                    continue
                kind, part = relation
                mime = self.overrides.get(part, self.defaults.get(part.rsplit(".", 1)[-1].lower(), ""))
                readable = kind.endswith("/image") and mime in IMAGE_MIMES and part in self.parts
                item = {"reason": "visual_not_read" if readable else "unsupported_media",
                        "readable": bool(readable), "_part": part}
                if transformed:
                    item["display_transform_unparsed"] = True
                if native:
                    item["preview_only"] = True
                yield item
            return
        if node.tag in {f"{{{W}}}del", f"{{{W}}}ins", f"{{{W}}}altChunk",
                        f"{{{W}}}object", f"{{{W}}}txbxContent"}:
            yield {"reason": "non_body_content", "readable": False}
            return
        for child in node:
            yield from self._objects(child)

    def unit(self, number: int) -> dict:
        block = self.blocks[number - 1]
        visuals = list(self._objects(block))
        if block.tag not in {f"{{{W}}}p", f"{{{W}}}tbl", f"{{{W}}}sectPr"}:
            visuals.append({"reason": "unsupported_block", "readable": False})
        for index, item in enumerate(visuals):
            item["image_ref"] = f"{self.version}:{number}:{index}"
        return {"text": self._text(block), "visuals": visuals}


def _paginate(total: int, getter, options: dict, *, unparsed=()) -> dict:
    start, offset, visual_offset = options["start"], options["offset"], options["visual_offset"]
    if start > total or total == 0:
        if start == 1 and not total and not offset and not visual_offset:
            return {"status": "partial", "reason": "empty_document", "content": "", "units": [],
                    "unparsed": list(unparsed), "total_units": 0, "next_cursor": None}
        raise DecodeError("invalid_cursor")
    result = {"status": "partial", "reason": "local_only", "content": "本地文字；视觉内容尚未读取。",
              "units": [], "unparsed": list(unparsed), "total_units": total, "next_cursor": None}
    visual_count = 0

    def cursor(number, text_offset, object_offset):
        return {"start": number, "offset": text_offset, "visual_offset": object_offset,
                "source_version": options["source_version"]}

    def fits():
        return len(_json(result)) <= MAX_WORKER_RESULT_CHARS

    end = min(total + 1, start + options["count"])
    for number in range(start, end):
        unit = getter(number)
        text, objects = unit["text"], unit["visuals"]
        if offset > len(text) or visual_offset > len(objects):
            raise DecodeError("invalid_cursor")
        result["next_cursor"] = cursor(number, offset, visual_offset)
        output = {"unit": number, "offset": offset, "text": "", "visual_offset": visual_offset, "visuals": []}
        result["units"].append(output)
        if not fits():
            result["units"].pop()
            return result
        lo, hi = 0, min(len(text) - offset, MAX_WORKER_RESULT_CHARS)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            output["text"] = text[offset:offset + middle]
            if fits():
                lo = middle
            else:
                hi = middle - 1
        output["text"] = text[offset:offset + lo]
        offset += lo
        while visual_offset < len(objects) and visual_count < MAX_VISUALS_PER_RESULT:
            visible = {key: value for key, value in objects[visual_offset].items() if not key.startswith("_")}
            output["visuals"].append(visible)
            if not fits():
                output["visuals"].pop()
                break
            visual_offset += 1
            visual_count += 1
        if offset < len(text) or visual_offset < len(objects):
            result["next_cursor"] = cursor(number, offset, visual_offset)
            return result
        offset = visual_offset = 0
        result["next_cursor"] = cursor(number + 1, 0, 0) if number < total else None
    return result


def _pdf(data: bytes, options: dict) -> tuple[dict, bytes | None]:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as pdf:
        if len(pdf) > MAX_PDF_PAGES:
            raise DecodeError("page_limit")
        form_type = pdf.get_formtype()
        extra = ["pdf_forms_unparsed"] if form_type else []

        def unit(number):
            with closing(pdf[number - 1]) as page:
                with closing(page.get_textpage()) as textpage:
                    if textpage.count_chars() > MAX_PAGE_CHARS:
                        return {"text": "", "visuals": [{"page": number, "reason": "page_text_limit", "readable": True}]}
                    text = textpage.get_text_bounded().replace("\r\n", "\n")
            return {"text": text, "visuals": [{"page": number, "reason": "visual_not_read", "readable": True}]}

        if options["operation"] == "read":
            return _paginate(len(pdf), unit, options, unparsed=extra), None
        number = options["page"]
        if number > len(pdf):
            raise DecodeError("invalid_page")
        if form_type:
            raise DecodeError("pdf_forms_unparsed")
        with closing(pdf[number - 1]) as page:
            width, height = page.get_size()
            if not all(math.isfinite(x) and 0 < x <= 1_000_000 for x in (width, height)):
                raise DecodeError("image_limit")
            x0, y0, x1, y1 = options.get("crop") or (0, 0, 1, 1)
            region_width, region_height = (x1 - x0) * width, (y1 - y0) * height
            scale = min(MAX_RENDER_EDGE / max(region_width, region_height),
                        math.sqrt(MAX_RENDER_PIXELS / (region_width * region_height)))
            # Extreme zoom can overflow PDFium's full-canvas coordinates even for a tiny crop.
            if scale * max(width, height) > 1_000_000:
                raise DecodeError("image_limit")
            crop = (x0 * width, (1 - y1) * height, (1 - x1) * width, y0 * height)
            with closing(page.render(scale=scale, crop=crop, draw_annots=True, may_draw_forms=False,
                                     fill_color=(255, 255, 255, 255))) as bitmap:
                with bitmap.to_pil() as image:
                    encoded = _png(image, orient=False)
        return {"status": "ok", "unparsed": [], "page": number}, encoded


def decode(data: bytes, options: dict) -> tuple[dict, bytes | None]:
    """Decode only the selected local unit; never call a network service."""
    kind = options["kind"]
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise DecodeError("invalid_document")
        return _pdf(data, options)
    if kind == "docx":
        doc = _Docx(data, options["source_version"])
        try:
            if options["operation"] == "read":
                return _paginate(len(doc.blocks), doc.unit, options, unparsed=doc.global_unparsed), None
            _, block, index = options["image_ref"].split(":")
            block, index = int(block), int(index)
            if not 1 <= block <= len(doc.blocks):
                raise DecodeError("invalid_image_ref")
            visuals = doc.unit(block)["visuals"]
            if not 0 <= index < len(visuals):
                raise DecodeError("invalid_image_ref")
            visual = visuals[index]
            if not visual["readable"]:
                raise DecodeError(visual["reason"])
            with _image(doc.read(visual["_part"], MAX_IMAGE_BYTES)) as image:
                encoded = _png(image, options.get("crop"))
            missing = ["display_transform_unparsed"] if visual.get("display_transform_unparsed") else []
            if visual.get("preview_only"):
                missing.append("preview_only")
            return {"status": "partial" if missing else "ok", "unparsed": missing}, encoded
        finally:
            doc.close()
    with _image(data) as image:
        encoded = _png(image, options.get("crop"))
    return {"status": "ok", "unparsed": []}, encoded


def _limits() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PNG_BYTES + MAX_WORKER_RESULT_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    except (ImportError, ValueError, OSError):
        pass


def main() -> None:
    """Fixed isolated-process entry; filenames are host-created, never model-selected."""
    _limits()
    directory = Path(sys.argv[1])
    try:
        options = json.loads((directory / "request.json").read_bytes())
        data = (directory / "input.bin").read_bytes()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise DecodeError("file_limit")
        result, image = decode(data, options)
        if image is not None:
            with (directory / "image.png").open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(image)
    except DecodeError as exc:
        result = {"status": "unavailable", "reason": str(exc), "content": "", "unparsed": [], "next_cursor": None}
    except Exception:
        # Third-party parser failures are an untrusted boundary; never return their diagnostics.
        result = {"status": "unavailable", "reason": "parse_failed", "content": "", "unparsed": [], "next_cursor": None}
    payload = _json(result).encode("utf-8")
    if len(payload) > MAX_WORKER_RESULT_BYTES:
        payload = b'{"status":"unavailable","reason":"result_limit"}'
    with (directory / "result.json").open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(payload)


if __name__ == "__main__":
    main()
