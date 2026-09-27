import hashlib
import os
import stat
import struct
import threading

BLOCK_SIZE = 1048576
WAD_MAGIC = b'RW'
SIGNATURE_SIZE = 256
HEADER_SIZE = 272
ENTRY_SIZE = 32
ENTRY_FORMAT = '<QIIIBBHQ'
ENTRY_STRUCT = struct.Struct(ENTRY_FORMAT)


class WADEntry:
    __slots__ = ('path_hash', 'data_offset', 'compressed_size',
                 'uncompressed_size', 'compression', 'subchunk_count',
                 'is_duplicate', 'first_subchunk_index', 'checksum')

    def __init__(self, path_hash, data_offset, compressed_size,
                 uncompressed_size, compression, subchunk_count,
                 is_duplicate, first_subchunk_index, checksum):
        self.path_hash = path_hash
        self.data_offset = data_offset
        self.compressed_size = compressed_size
        self.uncompressed_size = uncompressed_size
        self.compression = compression
        self.subchunk_count = subchunk_count
        self.is_duplicate = is_duplicate
        self.first_subchunk_index = first_subchunk_index
        self.checksum = checksum

    def to_dict(self):
        return {
            'path_hash': format(self.path_hash, '016x'),
            'compressed_size': self.compressed_size,
            'uncompressed_size': self.uncompressed_size,
            'compression': self.compression,
            'subchunk_count': self.subchunk_count,
            'is_duplicate': self.is_duplicate,
            'first_subchunk_index': self.first_subchunk_index,
            'checksum': format(self.checksum, '016x'),
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            path_hash=int(d['path_hash'], 16),
            data_offset=0,
            compressed_size=d['compressed_size'],
            uncompressed_size=d['uncompressed_size'],
            compression=d['compression'],
            subchunk_count=d['subchunk_count'],
            is_duplicate=d['is_duplicate'],
            first_subchunk_index=d['first_subchunk_index'],
            checksum=int(d['checksum'], 16),
        )


def parse_wad(path):
    """-> (major, minor, checksum, entries)"""
    with open(path, 'rb') as f:
        magic = f.read(2)
        if magic != WAD_MAGIC:
            raise ValueError(f'Not a WAD file: {path}')

        major, minor = struct.unpack('BB', f.read(2))
        f.seek(SIGNATURE_SIZE, 1)

        file_checksum, entry_count = struct.unpack('<QI', f.read(12))

        entry_data = f.read(entry_count * ENTRY_SIZE)
        entries = []
        for i in range(entry_count):
            (path_hash, data_offset, compressed_size, uncompressed_size,
             type_byte, is_duplicate, first_subchunk_index,
             checksum) = ENTRY_STRUCT.unpack_from(entry_data, i * ENTRY_SIZE)
            entries.append(WADEntry(
                path_hash, data_offset, compressed_size, uncompressed_size,
                type_byte & 0x0F, (type_byte >> 4) & 0x0F,
                is_duplicate, first_subchunk_index, checksum
            ))

    return major, minor, file_checksum, entries


def hash_bytes(data):
    """sha256 hex"""
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def unpack_wad(wad_path, repo_files_dir):
    """old v1 format. rebuilds aren't byte exact (signature gets zeroed), only kept so old repos load"""
    major, minor, file_checksum, entries = parse_wad(wad_path)

    manifest = {
        '__wad_manifest__': True,
        'version_major': major,
        'version_minor': minor,
        'entries': [],
    }
    blob_hashes = set()

    with open(wad_path, 'rb') as f:
        for entry in entries:
            f.seek(entry.data_offset)
            blob = f.read(entry.compressed_size)
            blob_hash = hash_bytes(blob)
            blob_hashes.add(blob_hash)

            dst_path = os.path.join(repo_files_dir, blob_hash)
            if not os.path.exists(dst_path):
                tmp = dst_path + '.tmp'
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
                    try:
                        os.write(fd, blob)
                    finally:
                        os.close(fd)
                    os.replace(tmp, dst_path)
                    os.chmod(dst_path, stat.S_IREAD)
                except (PermissionError, FileExistsError):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

            entry_dict = entry.to_dict()
            entry_dict['data_hash'] = blob_hash
            manifest['entries'].append(entry_dict)

    return manifest, blob_hashes


def pack_wad(manifest, repo_files_dir, output_path):
    """v1 rebuild. see above"""
    major = manifest['version_major']
    minor = manifest['version_minor']
    entries_data = manifest['entries']
    entry_count = len(entries_data)

    data_offset = HEADER_SIZE + (entry_count * ENTRY_SIZE)

    hash_to_offset = {}
    hash_to_size = {}
    unique_hashes_ordered = []
    rebuilt_entries = []

    for ed in entries_data:
        entry = WADEntry.from_dict(ed)
        data_hash = ed['data_hash']

        if data_hash in hash_to_offset:
            entry.data_offset = hash_to_offset[data_hash]
            entry.compressed_size = hash_to_size[data_hash]
        else:
            blob_path = os.path.join(repo_files_dir, data_hash)
            blob_size = os.path.getsize(blob_path)
            entry.data_offset = data_offset
            entry.compressed_size = blob_size
            hash_to_offset[data_hash] = data_offset
            hash_to_size[data_hash] = blob_size
            unique_hashes_ordered.append(data_hash)
            data_offset += blob_size

        rebuilt_entries.append(entry)

    entry_table = bytearray(entry_count * ENTRY_SIZE)
    for i, entry in enumerate(rebuilt_entries):
        type_byte = (entry.compression & 0x0F) | ((entry.subchunk_count & 0x0F) << 4)
        ENTRY_STRUCT.pack_into(entry_table, i * ENTRY_SIZE,
                               entry.path_hash, entry.data_offset,
                               entry.compressed_size, entry.uncompressed_size,
                               type_byte, entry.is_duplicate,
                               entry.first_subchunk_index, entry.checksum)

    file_checksum = _compute_file_checksum(rebuilt_entries)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, 'wb', buffering=4 * 1024 * 1024) as f:
        f.write(WAD_MAGIC)
        f.write(struct.pack('BB', major, minor))
        f.write(b'\x00' * SIGNATURE_SIZE)
        f.write(struct.pack('<QI', file_checksum, entry_count))
        f.write(entry_table)

        for data_hash in unique_hashes_ordered:
            blob_path = os.path.join(repo_files_dir, data_hash)
            blob_size = hash_to_size[data_hash]
            fd = os.open(blob_path, os.O_RDONLY | os.O_BINARY)
            try:
                if blob_size <= BLOCK_SIZE:
                    f.write(os.read(fd, blob_size))
                else:
                    remaining = blob_size
                    while remaining > 0:
                        chunk = os.read(fd, min(BLOCK_SIZE, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
            finally:
                os.close(fd)


def _compute_file_checksum(entries):
    """wad checksum = xxh64 over the entry table sorted by offset"""
    try:
        import xxhash
    except ImportError:
        return 0

    sorted_entries = sorted(entries, key=lambda e: e.data_offset)
    buf = bytearray(len(sorted_entries) * ENTRY_SIZE)
    for i, entry in enumerate(sorted_entries):
        type_byte = (entry.compression & 0x0F) | ((entry.subchunk_count & 0x0F) << 4)
        ENTRY_STRUCT.pack_into(buf, i * ENTRY_SIZE,
                               entry.path_hash, entry.data_offset,
                               entry.compressed_size, entry.uncompressed_size,
                               type_byte, entry.is_duplicate,
                               entry.first_subchunk_index, entry.checksum)
    return xxhash.xxh64(buf).intdigest()


MANIFEST_PROBE = b'{"__wad_manifest__"'
_INLINE_MAX = 64 * 1024 * 1024


_claims_lock = threading.Lock()
_inflight = {}


# two threads can hit the same asset at once. first claim writes it, the rest wait
def _claim(digest, known):
    """True = you write it. False = someone else did and we waited for them"""
    with _claims_lock:
        if digest in known:
            return False
        event = _inflight.get(digest)
        if event is None:
            _inflight[digest] = threading.Event()
            return True
    event.wait()
    if digest not in known:
        raise ValueError(f'Segment {digest[:12]} failed to store')
    return False


def _release(digest, known, ok):
    with _claims_lock:
        if ok:
            known.add(digest)
        _inflight.pop(digest).set()


def _commit_tmp(tmp, dst):
    try:
        os.replace(tmp, dst)
        os.chmod(dst, stat.S_IREAD)
    except PermissionError:
        os.unlink(tmp)
        if not os.path.exists(dst):
            raise


def _write_blob(files_dir, digest, data):
    dst = os.path.join(files_dir, digest)
    tmp = f'{dst}.{threading.get_ident()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
    try:
        os.write(fd, data)
        os.fsync(fd)  # it's trusted once it has its name, so it has to really be there first
    finally:
        os.close(fd)
    _commit_tmp(tmp, dst)


def _store_region(f, size, files_dir, known, whole, writer=None):
    if size <= _INLINE_MAX:
        data = f.read(size)
        if len(data) != size:
            raise ValueError('Unexpected end of file')
        whole.update(data)
        digest = hashlib.sha256(data).hexdigest()
        if _claim(digest, known):
            ok = False
            try:
                if writer is not None and size <= writer.max_size:
                    writer.add(digest, data)
                else:
                    _write_blob(files_dir, digest, data)
                ok = True
            finally:
                _release(digest, known, ok)
        return digest

    tmp = os.path.join(files_dir, f'stream.{threading.get_ident()}.tmp')
    h = hashlib.sha256()
    with open(tmp, 'wb') as out:
        remaining = size
        while remaining:
            chunk = f.read(min(BLOCK_SIZE, remaining))
            if not chunk:
                raise ValueError('Unexpected end of file')
            h.update(chunk)
            whole.update(chunk)
            out.write(chunk)
            remaining -= len(chunk)
        out.flush()
        os.fsync(out.fileno())
    digest = h.hexdigest()
    try:
        must_write = _claim(digest, known)
    except ValueError:
        os.unlink(tmp)
        raise
    if not must_write:
        os.unlink(tmp)
        return digest
    ok = False
    try:
        _commit_tmp(tmp, os.path.join(files_dir, digest))
        ok = True
    finally:
        _release(digest, known, ok)
    return digest


# riot pads between entries with zeros. store the length, not the zeros
def _store_gap(f, size, files_dir, known, whole, writer=None):
    remaining, zero = size, True
    start = f.tell()
    while remaining:
        chunk = f.read(min(BLOCK_SIZE, remaining))
        if not chunk:
            raise ValueError('Unexpected end of file')
        if zero and chunk.count(0) != len(chunk):
            zero = False
        remaining -= len(chunk)
    if zero:
        _update_zeros(whole, size)
        return None
    f.seek(start)
    return _store_region(f, size, files_dir, known, whole, writer)


def _update_zeros(h, size):
    block = bytes(BLOCK_SIZE)
    while size:
        n = min(size, BLOCK_SIZE)
        h.update(block[:n])
        size -= n


def unpack_wad_exact(wad_path, files_dir, known, expected_sha=None, writer=None):
    """split a wad into pieces that glue back into the exact same bytes.
    None if the layout is something we don't handle"""
    size = os.path.getsize(wad_path)
    major, _, _, entries = parse_wad(wad_path)
    if major != 3:
        return None
    regions = sorted({(e.data_offset, e.compressed_size) for e in entries if e.compressed_size})
    toc_end = HEADER_SIZE + len(entries) * ENTRY_SIZE
    head_end = regions[0][0] if regions else size
    if head_end < toc_end:
        return None

    layout = [('data', 0, head_end)]
    pos = head_end
    for off, sz in regions:
        if off < pos:
            return None
        if off > pos:
            layout.append(('gap', pos, off - pos))
        layout.append(('data', off, sz))
        pos = off + sz
    if pos > size:
        return None
    if pos < size:
        layout.append(('gap', pos, size - pos))

    whole = hashlib.sha256()
    segments = []
    with open(wad_path, 'rb', buffering=BLOCK_SIZE) as f:
        for kind, _, sz in layout:
            if sz == 0:
                continue
            store = _store_region if kind == 'data' else _store_gap
            segments.append([store(f, sz, files_dir, known, whole, writer), sz])

    digest = whole.hexdigest()
    if expected_sha and digest != expected_sha:
        raise ValueError('Rebuilt data does not match the original file')
    return {
        '__wad_manifest__': True,
        'format': 2,
        'sha256': digest,
        'size': size,
        'segments': segments,
    }


class ArchiveMismatch(ValueError):
    """what came out of storage isn't the archive that went in"""


def _exact_chunks(manifest, files_dir, packs=None, run=4 * 1024 * 1024):
    """the original archive's bytes, a chunk at a time, straight from storage. pieces sitting back to back
    in a bundle come out of one read of up to run bytes. after rearranging storage that's most of an
    archive, and one big read beats hundreds of small ones, on a hard drive by a mile"""
    zeros = bytes(BLOCK_SIZE)
    segments = manifest['segments']
    i = 0
    while i < len(segments):
        digest, size = segments[i]
        i += 1
        if digest is None:
            while size:
                n = min(size, BLOCK_SIZE)
                yield zeros[:n]
                size -= n
            continue
        where = packs.index.get(digest) if packs is not None else None
        if where is not None and where[2] > run:
            # one piece bigger than a whole read, rearranging can bundle those. a bit at a time, or 16 threads
            # each holding a 100 MB piece is a lot of memory for nothing
            base, start, size = where
            for off in range(start, start + size, run):
                try:
                    yield packs.read_from(base, off, min(run, start + size - off))
                except ValueError:
                    raise ArchiveMismatch(f'a stored piece is cut short ({digest[:12]})') from None
            continue
        if where is not None:
            base, start, first = where
            sizes, end = [first], start + first
            while i < len(segments) and end - start < run:
                nxt = segments[i][0] and packs.index.get(segments[i][0])
                if not nxt or nxt[0] != base or nxt[1] != end:
                    break
                sizes.append(nxt[2])
                end += nxt[2]
                i += 1
            try:
                data = memoryview(packs.read_from(base, start, end - start))
            except ValueError:
                # the bundle's been cut short. counts as damage, same as a missing piece
                raise ArchiveMismatch(f'a stored piece is cut short ({digest[:12]})') from None
            pos = 0
            for n in sizes:
                yield data[pos:pos + n]
                pos += n
            continue
        try:
            fd = os.open(os.path.join(files_dir, digest), os.O_RDONLY | os.O_BINARY)
        except FileNotFoundError:
            raise ArchiveMismatch(f'a stored piece is missing ({digest[:12]})') from None
        try:
            remaining = size
            while remaining:
                chunk = os.read(fd, min(run, remaining))
                if not chunk:
                    raise ArchiveMismatch(f'a stored piece is cut short ({digest[:12]})')
                yield chunk
                remaining -= len(chunk)
        finally:
            os.close(fd)


def pack_wad_exact(manifest, files_dir, output_path, packs=None, progress=None, run=4 * 1024 * 1024,
                   buffer=BLOCK_SIZE):
    """rebuild the archive, checking it against the original's fingerprint as it's written. costs a second
    or two on a whole patch and means a damaged storage folder gets caught here, not as a game crash.
    progress(bytes) gets called every few mb, so big archives don't sit at 0 until they're done.
    run and buffer are how much gets read and written at a time, bigger for a hard drive"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    expected = manifest.get('sha256')
    h = hashlib.sha256() if expected else None
    unreported = 0
    with open(output_path, 'wb', buffering=buffer) as out:
        for chunk in _exact_chunks(manifest, files_dir, packs, run):
            out.write(chunk)
            if h:
                h.update(chunk)
            unreported += len(chunk)
            if progress and unreported >= BLOCK_SIZE * 8:
                progress(unreported)
                unreported = 0
    if progress and unreported:
        progress(unreported)
    if h and h.hexdigest() != expected:
        raise ArchiveMismatch("it doesn't match the original")


def exact_digest(manifest, files_dir, packs=None):
    """the fingerprint of the archive storage would build, without writing it anywhere"""
    h = hashlib.sha256()
    for chunk in _exact_chunks(manifest, files_dir, packs):
        h.update(chunk)
    return h.hexdigest()


def manifest_hashes(manifest):
    if 'segments' in manifest:
        return {d for d, _ in manifest['segments'] if d}
    return {e['data_hash'] for e in manifest['entries']}


def estimate_toc(wad_path):
    """cheap dedup guess from the entry table, no data reads"""
    major, _, _, entries = parse_wad(wad_path)
    toc = HEADER_SIZE + len(entries) * ENTRY_SIZE
    keys = {(e.checksum, e.compressed_size) for e in entries if e.compressed_size}
    return toc, keys


