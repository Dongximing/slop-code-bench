/**
 * Dynamic Preprocessor - Streaming data processor with caching support.
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
    "name": {
      "type": "identity",
      "source": "name"
    }
  },
  "filter_conditions": [
    {
      "type": "equality",
      "column": "id",
      "operator": "!=",
      "value": 2
    },
    {
      "type": "equality",
      "column": "name",
      "operator": "!=",
      "value": "Bob"
    },
    {
      "type": "equality",
      "column": "age",
      "operator": "!=",
      "value": 19
    },
    {
      "type": "equality",
      "column": "active",
      "operator": "!=",
      "value": false
    }
  ],
  "input_columns": [
    "id",
    "name",
    "age",
    "active"
  ],
  "output_columns": [
    "id",
    "name"
  ],
  "row_mapping": {
    "0": 0,
    "1": 2
  }
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
    if (row['id'] === 2) return false;
    if (row['name'] === 'Bob') return false;
    if (row['age'] === 19) return false;
    if (row['active'] === false) return false;
    return true;
}

function transformRow(row) {
    const result = {};
    result['id'] = row['id'];
    result['name'] = row['name'];
    return result;
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

function loadCache(cachePath) {
    if (!cachePath || !fs.existsSync(cachePath)) {
        return { processedRows: 0, rows: [] };
    }
    return JSON.parse(fs.readFileSync(cachePath, 'utf-8'));
}

function saveCache(cachePath, state) {
    if (!cachePath) return;
    fs.writeFileSync(cachePath, JSON.stringify(state));
}

class DynamicPreprocessor {
    constructor({ buffer, cache_dir = null }) {
        this.buffer = buffer;
        this.cacheDir = cache_dir;
    }

    process(filePath) {
        const self = this;
        return {
            [Symbol.iterator]: function* () {
                const cachePath = getCachePath(filePath, self.cacheDir);
                const cacheState = loadCache(cachePath);
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
                        saveCache(cachePath, cacheState);
                        continue;
                    }

                    const transformed = transformRow(row);

                    if (cachePath) {
                        currentCacheRows.push(transformed);
                        bufferCount++;

                        if (bufferCount >= self.buffer) {
                            cacheState.processedRows = rowCount + 1;
                            cacheState.rows = currentCacheRows;
                            saveCache(cachePath, cacheState);
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
                    saveCache(cachePath, cacheState);
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
