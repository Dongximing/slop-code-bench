"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");


function _parseValue(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v;
  const s = String(v);
  const n = Number(s);
  if (!isNaN(n) && s.trim() !== "") return n;
  if (s.toLowerCase() === "true") return true;
  if (s.toLowerCase() === "false") return false;
  if (s.toLowerCase() === "null" || s === "") return null;
  return s;
}


function _tryFloat(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return null;
  if (typeof v === "number") return v;
  const n = Number(v);
  return isNaN(n) ? null : n;
}


class _StreamState {
  constructor() {
    this.prefixSums = {};
    this.prefixCounts = {};
    this.windows = {};
    this.stateValues = {};
    this.prevValues = {};
    this.prevRow = null;
    this.pendingRow = null;
    this.rowIndex = 0;
  }

  toObject() {
    return {
      prefixSums: { ...this.prefixSums },
      prefixCounts: { ...this.prefixCounts },
      windows: Object.fromEntries(
        Object.entries(this.windows).map(([k, v]) => [k, [...v]])
      ),
      stateValues: { ...this.stateValues },
      prevValues: { ...this.prevValues },
      rowIndex: this.rowIndex,
    };
  }

  static fromObject(d) {
    const s = new _StreamState();
    s.prefixSums = d.prefixSums || {};
    s.prefixCounts = d.prefixCounts || {};
    s.windows = Object.fromEntries(
      Object.entries(d.windows || {}).map(([k, v]) => [k, v])
    );
    s.stateValues = d.stateValues || {};
    s.prevValues = d.prevValues || {};
    s.rowIndex = d.rowIndex || 0;
    return s;
  }
}


// Stateful transform configuration
const _PREFIX_RUNNING_TOTAL_SOURCE = "value";
const _PREFIX_RUNNING_TOTAL_TYPE = "prefix_sum";
const _PREFIX_RUNNING_TOTAL_A = 1;
const _PREFIX_RUNNING_TOTAL_B = 0;
const _NEIGHBOR_FILTER_TYPE = null;

function _transformRow(_parsed, _state) {
  const _result = {};
  // Update prefix sum for running_total
  const _val_running_total = _tryFloat(_parsed["value"]) || 0.0;
  if (!_state.prefixSums["running_total"]) _state.prefixSums["running_total"] = 0.0;
  _state.prefixSums["running_total"] += _val_running_total;
  _result["id"] = _parsed["id"];
  _result["running_total"] = _state.prefixSums["running_total"] * 1 + 0;
  return _result;
}


function _keepRow(_parsed) {
  return true;
}


function _neighborKeepRow(_parsed, _state) {
  return true;
}

const _FILE_EXT = "csv";


function* _readRows(filePath, ext) {
  const content = fs.readFileSync(filePath, "utf-8");
  if (ext === "csv" || ext === "tsv") {
    const lines = content.split(/\r?\n/).filter(l => l.trim() !== "");
    if (lines.length === 0) return;
    const sep = ext === "tsv" ? "\t" : ",";
    const headers = _parseCsvLine(lines[0], sep);
    for (let i = 1; i < lines.length; i++) {
      const vals = _parseCsvLine(lines[i], sep);
      const row = {};
      for (let j = 0; j < headers.length; j++) {
        row[headers[j]] = j < vals.length ? vals[j] : null;
      }
      yield row;
    }
  } else if (ext === "jsonl") {
    const lines = content.split(/\r?\n/).filter(l => l.trim() !== "");
    for (const line of lines) {
      yield JSON.parse(line);
    }
  } else if (ext === "json") {
    const data = JSON.parse(content);
    for (const row of data) {
      yield row;
    }
  }
}


function _parseCsvLine(line, sep) {
  const result = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (i + 1 < line.length && line[i + 1] === '"') {
          current += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        current += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (line.substring(i, i + sep.length) === sep) {
        result.push(current);
        current = "";
        i += sep.length - 1;
      } else {
        current += ch;
      }
    }
  }
  result.push(current);
  return result;
}


function DynamicPreprocessor({ buffer, cache_dir = null }) {
  return function(filePath) {
    return {
      [Symbol.iterator]() {
        return _iterate(filePath, buffer, cache_dir);
      }
    };
  };
}


function* _iterate(filePath, buffer, cache_dir) {
  const ext = _FILE_EXT;
  const cacheKey = crypto.createHash("sha256").update(path.resolve(filePath), "utf8").digest("hex");
  let cacheFile = null;
  let stateFile = null;
  let resumeFrom = 0;
  const _state = new _StreamState();

  if (cache_dir) {
    fs.mkdirSync(cache_dir, { recursive: true });
    cacheFile = path.join(cache_dir, "cache_" + cacheKey);
    stateFile = cacheFile + ".state";
    const indexFile = cacheFile + ".idx";
    if (fs.existsSync(indexFile)) {
      resumeFrom = parseInt(fs.readFileSync(indexFile, "utf-8").trim(), 10);
    }
    if (fs.existsSync(stateFile)) {
      try {
        const stateData = JSON.parse(fs.readFileSync(stateFile, "utf-8"));
        Object.assign(_state, _StreamState.fromObject(stateData));
      } catch (e) {}
    }
  }

  let rowCount = 0;
  let bufferCount = 0;

  for (const rawRow of _readRows(filePath, ext)) {
    const _parsed = {};
    for (const _k of Object.keys(rawRow)) {
      _parsed[_k] = _parseValue(rawRow[_k]);
    }

    if (!_keepRow(_parsed)) {
      _state.rowIndex++;
      continue;
    }

    if (!_neighborKeepRow(_parsed, _state)) {
      _state.rowIndex++;
      continue;
    }

    const _result = _transformRow(_parsed, _state);
    rowCount++;
    bufferCount++;
    _state.rowIndex++;

    if (rowCount <= resumeFrom) continue;

    if (cache_dir && cacheFile) {
      fs.writeFileSync(stateFile, JSON.stringify(_state.toObject()));
      fs.writeFileSync(cacheFile + ".idx", String(rowCount));
    }

    yield _result;

    if (buffer !== null && bufferCount >= buffer) {
      bufferCount = 0;
    }
  }
}


module.exports = { DynamicPreprocessor };
