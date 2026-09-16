const fs = require('fs');
const CACHE_DIR = __REPOWATCH_CACHE_DIR__;
const KEY_MARKER = '\nKEY: ';

function cachePath(hash) {
    return CACHE_DIR + '/' + hash.slice(-1) + '/' + hash.slice(-3, -1) + '/' + hash;
}

function probe(r) {
    const key = r.args.key;
    if (!key) { r.return(400, 'missing key\n'); return; }
    const hash = require('crypto').createHash('md5').update(key).digest('hex');
    let result;
    try {
        const st = fs.statSync(cachePath(hash));
        result = {exists: true, size: st.size, mtime: st.mtime};
    } catch (e) {
        if (e.code !== 'ENOENT') { r.return(500, 'cache stat failed\n'); return; }
        result = {exists: false};
    }
    r.headersOut['Content-Type'] = 'application/json';
    r.return(200, JSON.stringify(result));
}

function extractKey(buf) {
    const idx = buf.indexOf(KEY_MARKER);
    if (idx < 0) return null;
    const start = idx + KEY_MARKER.length;
    const nl = buf.indexOf('\n', start);
    if (nl < 0) return null;
    return buf.slice(start, nl).toString();
}

function readKey(full, buffer) {
    const fd = fs.openSync(full, 'r');
    try {
        let used = 0;
        while (used < buffer.length) {
            const count = fs.readSync(fd, buffer, used, Math.min(4096, buffer.length - used), used);
            if (count === 0) throw Error('cache key header missing or truncated');
            used += count;
            const key = extractKey(buffer.subarray(0, used));
            if (key !== null && key.length > 0) return key;
        }
        throw Error('cache key header exceeds 65536 bytes or is malformed');
    } finally {
        fs.closeSync(fd);
    }
}

function scan(r) {
    const dir = r.args.dir;
    if (!/^[0-9a-f]\/[0-9a-f]{2}$/.test(dir || '')) { r.return(400, 'bad dir\n'); return; }
    const dirPath = CACHE_DIR + '/' + dir;
    let entries = [];
    try { entries = fs.readdirSync(dirPath); } catch (e) {
        if (e.code !== 'ENOENT') { r.return(500, 'cache directory read failed\n'); return; }
    }
    // Reuse one bounded buffer per request; never read the package body in full.
    const buffer = Buffer.alloc(65536);
    const results = entries.map((name) => {
        const full = dirPath + '/' + name;
        let st;
        try { st = fs.statSync(full); } catch (e) {
            return {file: name, error: String(e)};
        }
        try {
            return {file: name, key: readKey(full, buffer), size: st.size, mtime: st.mtime};
        } catch (e) {
            return {file: name, size: st.size, mtime: st.mtime, error: String(e)};
        }
    });
    r.headersOut['Content-Type'] = 'application/json';
    r.return(200, JSON.stringify(results));
}

export default {probe, scan};
