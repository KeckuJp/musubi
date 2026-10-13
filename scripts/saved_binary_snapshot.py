"""Bounded saved regular-file mapping; contains no protocol decoder."""
import hashlib
import mmap
import os
from pathlib import Path
import stat
import tempfile

MAX_BYTES = 2**31


def copy_source(source, target):
    digest, total = hashlib.sha256(), 0
    while chunk := source.read(1024*1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError('saved snapshot size exceeded')
        digest.update(chunk)
        target.write(chunk)
    target.flush()
    return digest.hexdigest(), total


def convert_snapshot(path, converter, **options):
    path = Path(path)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError('saved regular file required')
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size,
                               value.st_mtime_ns, value.st_ctime_ns)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or signature(initial) != signature(before)
                or not 0 < before.st_size <= MAX_BYTES):
            raise ValueError('invalid saved snapshot identity or size')
        with tempfile.TemporaryFile(mode='w+b') as frozen:
            digest, total = copy_source(source, frozen)
            if (total != before.st_size
                    or signature(before) != signature(os.fstat(source.fileno()))
                    or signature(before) != signature(path.lstat())):
                raise ValueError('saved snapshot changed during copy')
            with mmap.mmap(frozen.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if digest != hashlib.sha256(data).hexdigest():
                    raise ValueError('saved snapshot copy digest mismatch')
                output, report = converter(data, _window=True, **options)
                if digest != report['source_sha256']:
                    raise ValueError('saved snapshot report digest mismatch')
        if (signature(before) != signature(os.fstat(source.fileno()))
                or signature(before) != signature(path.lstat())):
            raise ValueError('saved snapshot identity changed')
    report['snapshot_basis'] = 'READ_SNAPSHOT_ONLY_NOT_LATER_PATH_IMMUTABILITY'
    return output, report
