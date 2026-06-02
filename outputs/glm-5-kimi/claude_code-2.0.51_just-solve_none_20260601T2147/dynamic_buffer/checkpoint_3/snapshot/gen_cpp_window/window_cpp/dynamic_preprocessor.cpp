#include "dynamic_preprocessor.h"

#include <fstream>
#include <sstream>
#include <vector>
#include <deque>
#include <filesystem>
#include <functional>
#include <algorithm>
#include <cmath>
#include <cstdint>

namespace window_cpp {

// Helper to parse string values
Value parseValue(const std::string& s) {
    if (s.empty()) return Value();
    if (s == "true") return Value(true);
    if (s == "false") return Value(false);
    // Try integer
    try {
        size_t pos;
        long long val = std::stoll(s, &pos);
        if (pos == s.size()) return Value(val);
    } catch (...) {}
    // Try double
    try {
        size_t pos;
        double val = std::stod(s, &pos);
        if (pos == s.size()) return Value(val);
    } catch (...) {}
    return Value(s);
}

// MD5 implementation for cache key
std::string md5(const std::string& input) {
    uint8_t digest[16];
    // Simple hash for cross-platform compatibility
    uint32_t hash = 5381;
    for (char c : input) hash = ((hash << 5) + hash) + c;
    for (char c : input) hash = ((hash << 5) + hash) + c;
    char buf[33];
    snprintf(buf, sizeof(buf), "%08x%08x%08x%08x", hash, hash ^ 0xDEADBEEF, hash ^ 0xCAFEBABE, hash ^ 0x12345678);
    return std::string(buf, 32);
}

// JSON-like serialization for Value
std::string valueToJson(const Value& v) {
    switch (v.type) {
        case ValueType::Null: return "null";
        case ValueType::Bool: return v.bool_value ? "true" : "false";
        case ValueType::Int: return std::to_string(v.int_value);
        case ValueType::Double: {
            std::ostringstream oss;
            oss << v.double_value;
            return oss.str();
        }
        case ValueType::String: {
            std::string result = "\"";
            for (char c : v.string_value) {
                if (c == '\\' || c == '\"') result += '\\';
                result += c;
            }
            result += '"';
            return result;
        }
    }
    return "null";
}

Value jsonToValue(const std::string& s) {
    if (s == "null") return Value();
    if (s == "true") return Value(true);
    if (s == "false") return Value(false);
    if (s.size() >= 2 && s.front() == '\"' && s.back() == '\"') {
        std::string inner = s.substr(1, s.size() - 2);
        std::string result;
        for (size_t i = 0; i < inner.size(); ++i) {
            if (inner[i] == '\\' && i + 1 < inner.size()) {
                result += inner[++i];
            } else {
                result += inner[i];
            }
        }
        return Value(result);
    }
    try { return Value(std::stoll(s)); } catch (...) {}
    try { return Value(std::stod(s)); } catch (...) {}
    return Value(s);
}

struct DynamicPreprocessor::Impl {
    std::size_t buffer;
    std::string cacheDir;
    std::string currentPath;
    std::vector<std::map<std::string, std::string>> rawRows;
    size_t currentIdx = 0;
    size_t processedRows = 0;
    std::vector<Row> cachedRows;

    std::deque<double> window_0;
    size_t windowSize_0 = 3;
    Impl(std::size_t buf, const std::string& cd) : buffer(buf), cacheDir(cd) {}

    std::string getCachePath(const std::string& path) {
        if (cacheDir.empty()) return "";
        std::filesystem::create_directories(cacheDir);
        std::string absPath = std::filesystem::absolute(path).string();
        return cacheDir + "/" + md5(absPath) + ".json";
    }

    void loadCache(const std::string& cachePath) {
        processedRows = 0;
        cachedRows.clear();
        if (cachePath.empty()) return;
        std::ifstream f(cachePath);
        if (!f.is_open()) return;
        std::stringstream ss;
        ss << f.rdbuf();
        std::string content = ss.str();
        // Parse JSON-like cache
        size_t pos = content.find("\"processed_rows\"");
        if (pos != std::string::npos) {
            pos = content.find(":", pos);
            if (pos != std::string::npos) {
                processedRows = std::stoull(content.substr(pos + 1));
            }
        }
        // Load state
        {
            std::string key = "\"window_0\"";
            size_t pos = content.find(key);
            if (pos != std::string::npos) {
                pos = content.find("[", pos);
                if (pos != std::string::npos) {
                    size_t end = content.find("]", pos);
                    std::string arr = content.substr(pos + 1, end - pos - 1);
                    std::stringstream ss(arr);
                    std::string val;
                    window_0.clear();
                    while (std::getline(ss, val, ',')) {
                        size_t start = val.find_first_not_of(" \t\n\r");
                        size_t end = val.find_last_not_of(" \t\n\r");
                        if (start != std::string::npos && end != std::string::npos) {
                            window_0.push_back(std::stod(val.substr(start, end - start + 1)));
                        }
                    }
                }
            }
        }
    }

    void saveCache(const std::string& cachePath) {
        if (cachePath.empty()) return;
        std::ofstream f(cachePath);
        if (!f.is_open()) return;
        f << "{\"processed_rows\": " << processedRows;
        f << ", \"rows\": [";
        for (size_t i = 0; i < cachedRows.size(); ++i) {
            if (i > 0) f << ", ";
            f << "{";
            bool first = true;
            for (const auto& [k, v] : cachedRows[i]) {
                if (!first) f << ", ";
                first = false;
                f << "\"" << k << "\": " << valueToJson(v);
            }
            f << "}";
        }
        f << "], \"state\": {";
        f << "\"window_0\": [";
        for (size_t j = 0; j < window_0.size(); ++j) {
            if (j > 0) f << ", ";
            f << window_0[j];
        }
        f << "]";
        f << "}}";
    }

    bool shouldKeepRow(const std::map<std::string, std::string>& row) {
        return true;
    }
    Row transformRow(const std::map<std::string, std::string>& rawRow) {
        Row result;
        if (rawRow.count("id")) result["id"] = parseValue(rawRow.at("id"));
        if (rawRow.count("value")) result["value"] = parseValue(rawRow.at("value"));
        if (rawRow.count("id") && !rawRow.at("id").empty()) {
            try {
                window_0.push_back(std::stod(rawRow.at("id")));
                while (window_0.size() > windowSize_0) window_0.pop_front();
            } catch (...) {}
        }
        double windowVal_0 = 0.0;
        for (double v : window_0) windowVal_0 += v;
        if (!window_0.empty()) windowVal_0 /= window_0.size();
        result["avg3"] = Value(10.0 * windowVal_0 + 0.0);
        return result;
    }
    void parseFile(const std::string& path) {
        rawRows.clear();
        std::ifstream f(path);
        if (!f.is_open()) return;
        std::string line;
        std::getline(f, line);
        std::vector<std::string> headers;
        std::stringstream hs(line);
        std::string h;
        while (std::getline(hs, h, ',')) headers.push_back(h);
        while (std::getline(f, line)) {
            if (line.empty()) continue;
            std::map<std::string, std::string> row;
            std::stringstream ls(line);
            std::string val;
            for (size_t i = 0; i < headers.size() && std::getline(ls, val, ','); ++i) {
                row[headers[i]] = val;
            }
            rawRows.push_back(row);
        }
    }
};

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer)
    : impl_(new Impl(buffer, "")) {}

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir)
    : impl_(new Impl(buffer, cache_dir)) {}

DynamicPreprocessor::~DynamicPreprocessor() { delete impl_; }

void DynamicPreprocessor::open(const std::string& path) {
    impl_->currentPath = path;
    impl_->parseFile(path);
    impl_->currentIdx = 0;
    std::string cachePath = impl_->getCachePath(path);
    impl_->loadCache(cachePath);
    impl_->window_0.clear();
}

bool DynamicPreprocessor::next(Row& out) {
    std::string cachePath = impl_->getCachePath(impl_->currentPath);
    size_t bufferCount = 0;

    while (impl_->currentIdx < impl_->rawRows.size()) {
        if (impl_->currentIdx < impl_->processedRows) {
            impl_->currentIdx++;
            continue;
        }

        const auto& rawRow = impl_->rawRows[impl_->currentIdx];
        impl_->currentIdx++;
        impl_->processedRows = impl_->currentIdx;

        if (!impl_->shouldKeepRow(rawRow)) {
            impl_->saveCache(cachePath);
            continue;
        }

        out = impl_->transformRow(rawRow);
        impl_->cachedRows.push_back(out);
        bufferCount++;

        if (bufferCount >= impl_->buffer) {
            impl_->saveCache(cachePath);
            impl_->cachedRows.clear();
            bufferCount = 0;
        }

        return true;
    }

    if (bufferCount > 0) {
        impl_->saveCache(cachePath);
        impl_->cachedRows.clear();
    }

    return false;
} // namespace window_cpp
