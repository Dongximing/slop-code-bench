/**
 * Dynamic Preprocessor - Streaming data processor with caching support.
 * Supports stateful transforms and neighbor-based filtering.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const CONFIG = {
  "column_transforms": {
    "id": {
      "type": "identity",
      "source": "id"
    },
    "value": {
      "type": "identity",
      "source": "value"
    },
    "avg3": {
      "type": "stateful",
      "transform_index": 0
    }
  },
  "filter_conditions": [],
  "input_columns": [
    "id",
    "value"
  ],
  "output_columns": [
    "id",
    "value",
    "avg3"
  ],
  "row_mapping": {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4
  },
  "stateful_transforms": [
    {
      "type": "sliding_window",
      "source": "id",
      "window_size": 3,
      "operation": "mean",
      "a": 10.0,
      "b": 0.0,
      "output_column": "avg3"
    }
  ],
  "neighbor_filters": []
};

function parseValue(value) {
    if (value === '') return null;
    if (value.toLowerCase() === 'true') return true;
    if (value.toLowerCase() === 'false') return false;
    const intVal = parseInt(value, 10);
    if (!isNaN(intVal) && String(intVal) === value) return intVal;
    const floatVal = parseFloat(value);
    if (!isNaN(floatVal)) return floatVal;
    return value;
}

function shouldKeepRow(row) {
    return true;
}

function transformRow(row, state) {
    const result = {};
    result['id'] = row['id'];
    result['value'] = row['value'];
    const val_win_0 = row['id'];
    if (typeof val_win_0 === "number") {
        state.window_0.push(val_win_0);
        if (state.window_0.length > state.windowSize_0) {
            state.window_0.shift();
        }
    }
    const windowSum_0 = state.window_0.reduce((a, b) => a + b, 0);
    const windowVal_0 = state.window_0.length > 0 ? windowSum_0 / state.window_0.length : 0.0;
    result["avg3"] = 10.0 * windowVal_0 + 0.0;
    return result;
}

function getCurrentState(state) {
    return {
        window_0: state.window_0.slice(),
    };
}

function restoreState(savedState, state) {
    state.window_0 = savedState.window_0 || [];
}

function* parseFile(filePath) {
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.trim().split('\n');
    if (lines.length === 0) return;

    const headers = lines[0].split(',');
    for (let i = 1; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;
        const values = line.split(',');
        const row = {};
        for (let j = 0; j < headers.length; j++) {
            row[headers[j]] = j < values.length ? parseValue(values[j]) : null;
        }
        yield row;
    }
}

function getCachePath(inputPath, cacheDir) {
    if (!cacheDir) return null;
    const hash = crypto.createHash('md5').update(path.resolve(inputPath)).digest('hex');
    if (!fs.existsSync(cacheDir)) {
        fs.mkdirSync(cacheDir, { recursive: true });
    }
    return path.join(cacheDir, `${hash}.json`);
}

function loadCache(cachePath, state) {
    if (!cachePath || !fs.existsSync(cachePath)) {
        return { processedRows: 0, rows: [] };
    }
    const data = JSON.parse(fs.readFileSync(cachePath, 'utf-8'));
    if (data.state) {
        restoreState(data.state, state);
    }
    return data;
}

function saveCache(cachePath, state, processorState) {
    if (!cachePath) return;
    state.state = getCurrentState(processorState);
    fs.writeFileSync(cachePath, JSON.stringify(state));
}

class DynamicPreprocessor {
    constructor({ buffer, cache_dir = null }) {
        this.buffer = buffer;
        this.cacheDir = cache_dir;
        this.state = this._initState();
    }

    _initState() {
        return {
            window_0: [],
            windowSize_0: 3,
        };
    }

    process(filePath) {
        const self = this;
        return {
            [Symbol.iterator]: function* () {
                const cachePath = getCachePath(filePath, self.cacheDir);
                const cacheState = loadCache(cachePath, self.state);
                const startRow = cacheState.processedRows || 0;

                let rowCount = 0;
                let bufferCount = 0;
                const currentCacheRows = [];

                for (const row of parseFile(filePath)) {
                    if (rowCount < startRow) {
                        rowCount++;
                        continue;
                    }

                    if (!shouldKeepRow(row)) {
                        rowCount++;
                        cacheState.processedRows = rowCount;
                        saveCache(cachePath, cacheState, self.state);
                        continue;
                    }

                    const transformed = transformRow(row, self.state);

                    if (cachePath) {
                        currentCacheRows.push(transformed);
                        bufferCount++;

                        if (bufferCount >= self.buffer) {
                            cacheState.processedRows = rowCount + 1;
                            cacheState.rows = currentCacheRows;
                            saveCache(cachePath, cacheState, self.state);
                            currentCacheRows.length = 0;
                            bufferCount = 0;
                        }
                    }

                    rowCount++;
                    yield transformed;
                }

                if (cachePath && (bufferCount > 0 || rowCount > startRow)) {
                    cacheState.processedRows = rowCount;
                    cacheState.rows = currentCacheRows;
                    saveCache(cachePath, cacheState, self.state);
                }
            }
        };
    }

    [Symbol.iterator]() {
        throw new Error('Call process(filePath) to get an iterator');
    }
}

function createPreprocessor(options) {
    const preprocessor = new DynamicPreprocessor(options);
    return function(filePath) {
        return preprocessor.process(filePath);
    };
}

module.exports = { DynamicPreprocessor, createPreprocessor };
