#!/usr/bin/env python3
"""C++ code generator for streaming data processor modules."""

from typing import Dict, List

from base_generator import CodeGenerator, val_str


class CppCodeGenerator(CodeGenerator):
    """Generates C++ module code from transformation config."""

    def generate(self) -> Dict[str, str]:
        return {
            'dynamic_preprocessor.h': self._generate_header(),
            'dynamic_preprocessor.cpp': self._generate_implementation(),
        }

    def _generate_header(self) -> str:
        lines = [
            '#pragma once',
            '',
            '#include <map>',
            '#include <string>',
            '#include <optional>',
            '#include <cstddef>',
            '',
            f'namespace {self.module_name} {{',
            '',
            'enum class ValueType {',
            '    Null,',
            '    Bool,',
            '    Int,',
            '    Double,',
            '    String',
            '};',
            '',
            'struct Value {',
            '    ValueType type = ValueType::Null;',
            '    bool bool_value = false;',
            '    long long int_value = 0;',
            '    double double_value = 0.0;',
            '    std::string string_value;',
            '',
            '    Value() : type(ValueType::Null) {}',
            '    explicit Value(bool v) : type(ValueType::Bool), bool_value(v) {}',
            '    explicit Value(long long v) : type(ValueType::Int), int_value(v) {}',
            '    explicit Value(int v) : type(ValueType::Int), int_value(v) {}',
            '    explicit Value(double v) : type(ValueType::Double), double_value(v) {}',
            '    explicit Value(const std::string& v) : type(ValueType::String), string_value(v) {}',
            '    explicit Value(const char* v) : type(ValueType::String), string_value(v) {}',
            '};',
            '',
            'using Row = std::map<std::string, Value>;',
            '',
            'class DynamicPreprocessor {',
            'public:',
            '    explicit DynamicPreprocessor(std::size_t buffer);',
            '    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);',
            '    ~DynamicPreprocessor();',
            '',
            '    void open(const std::string& path);',
            '    bool next(Row& out);',
            '',
            'private:',
            '    struct Impl;',
            '    Impl* impl_;',
            '};',
            '',
            f'}} // namespace {self.module_name}',
            '',
        ]
        return '\n'.join(lines)

    def _generate_implementation(self) -> str:
        has_stateful = self.has_stateful
        has_neighbor_filters = self.has_neighbor_filters

        lines = [
            f'#include "dynamic_preprocessor.h"',
            '',
            '#include <fstream>',
            '#include <sstream>',
            '#include <vector>',
            '#include <deque>',
            '#include <filesystem>',
            '#include <functional>',
            '#include <algorithm>',
            '#include <cmath>',
            '#include <cstdint>',
            '',
            f'namespace {self.module_name} {{',
            '',
            '// Helper to parse string values',
            'Value parseValue(const std::string& s) {',
            '    if (s.empty()) return Value();',
            '    if (s == "true") return Value(true);',
            '    if (s == "false") return Value(false);',
            '    // Try integer',
            '    try {',
            '        size_t pos;',
            '        long long val = std::stoll(s, &pos);',
            '        if (pos == s.size()) return Value(val);',
            '    } catch (...) {}',
            '    // Try double',
            '    try {',
            '        size_t pos;',
            '        double val = std::stod(s, &pos);',
            '        if (pos == s.size()) return Value(val);',
            '    } catch (...) {}',
            '    return Value(s);',
            '}',
            '',
            '// MD5 implementation for cache key',
            'std::string md5(const std::string& input) {',
            '    uint8_t digest[16];',
            '    // Simple hash for cross-platform compatibility',
            '    uint32_t hash = 5381;',
            '    for (char c : input) hash = ((hash << 5) + hash) + c;',
            '    for (char c : input) hash = ((hash << 5) + hash) + c;',
            '    char buf[33];',
            '    snprintf(buf, sizeof(buf), "%08x%08x%08x%08x", hash, hash ^ 0xDEADBEEF, hash ^ 0xCAFEBABE, hash ^ 0x12345678);',
            '    return std::string(buf, 32);',
            '}',
            '',
            '// JSON-like serialization for Value',
            'std::string valueToJson(const Value& v) {',
            '    switch (v.type) {',
            '        case ValueType::Null: return "null";',
            '        case ValueType::Bool: return v.bool_value ? "true" : "false";',
            '        case ValueType::Int: return std::to_string(v.int_value);',
            '        case ValueType::Double: {',
            '            std::ostringstream oss;',
            '            oss << v.double_value;',
            '            return oss.str();',
            '        }',
            '        case ValueType::String: {',
            '            std::string result = "\\"";',
            '            for (char c : v.string_value) {',
            '                if (c == \'\\\\\' || c == \'\\"\') result += \'\\\\\';',
            '                result += c;',
            '            }',
            '            result += \'"\';',
            '            return result;',
            '        }',
            '    }',
            '    return "null";',
            '}',
            '',
            'Value jsonToValue(const std::string& s) {',
            '    if (s == "null") return Value();',
            '    if (s == "true") return Value(true);',
            '    if (s == "false") return Value(false);',
            '    if (s.size() >= 2 && s.front() == \'\\"\' && s.back() == \'\\"\') {',
            '        std::string inner = s.substr(1, s.size() - 2);',
            '        std::string result;',
            '        for (size_t i = 0; i < inner.size(); ++i) {',
            '            if (inner[i] == \'\\\\\' && i + 1 < inner.size()) {',
            '                result += inner[++i];',
            '            } else {',
            '                result += inner[i];',
            '            }',
            '        }',
            '        return Value(result);',
            '    }',
            '    try { return Value(std::stoll(s)); } catch (...) {}',
            '    try { return Value(std::stod(s)); } catch (...) {}',
            '    return Value(s);',
            '}',
            '',
            'struct DynamicPreprocessor::Impl {',
            '    std::size_t buffer;',
            '    std::string cacheDir;',
            '    std::string currentPath;',
            '    std::vector<std::map<std::string, std::string>> rawRows;',
            '    size_t currentIdx = 0;',
            '    size_t processedRows = 0;',
            '    std::vector<Row> cachedRows;',
            '',
        ]

        if has_stateful:
            lines.extend(self._generate_cpp_state_vars())

        lines.extend([
            '    Impl(std::size_t buf, const std::string& cd) : buffer(buf), cacheDir(cd) {}',
            '',
            '    std::string getCachePath(const std::string& path) {',
            '        if (cacheDir.empty()) return "";',
            '        std::filesystem::create_directories(cacheDir);',
            '        std::string absPath = std::filesystem::absolute(path).string();',
            '        return cacheDir + "/" + md5(absPath) + ".json";',
            '    }',
            '',
            '    void loadCache(const std::string& cachePath) {',
            '        processedRows = 0;',
            '        cachedRows.clear();',
            '        if (cachePath.empty()) return;',
            '        std::ifstream f(cachePath);',
            '        if (!f.is_open()) return;',
            '        std::stringstream ss;',
            '        ss << f.rdbuf();',
            '        std::string content = ss.str();',
            '        // Parse JSON-like cache',
            '        size_t pos = content.find("\\"processed_rows\\"");',
            '        if (pos != std::string::npos) {',
            '            pos = content.find(":", pos);',
            '            if (pos != std::string::npos) {',
            '                processedRows = std::stoull(content.substr(pos + 1));',
            '            }',
            '        }',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_load())

        lines.extend([
            '    }',
            '',
            '    void saveCache(const std::string& cachePath) {',
            '        if (cachePath.empty()) return;',
            '        std::ofstream f(cachePath);',
            '        if (!f.is_open()) return;',
            '        f << "{\\"processed_rows\\": " << processedRows;',
            '        f << ", \\"rows\\": [";',
            '        for (size_t i = 0; i < cachedRows.size(); ++i) {',
            '            if (i > 0) f << ", ";',
            '            f << "{";',
            '            bool first = true;',
            '            for (const auto& [k, v] : cachedRows[i]) {',
            '                if (!first) f << ", ";',
            '                first = false;',
            '                f << "\\"" << k << "\\": " << valueToJson(v);',
            '            }',
            '            f << "}";',
            '        }',
            '        f << "], \\"state\\": {";',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_save())

        lines.extend([
            '        f << "}}";',
            '    }',
            '',
        ])

        lines.extend(self._generate_cpp_should_keep_row())
        lines.extend(self._generate_cpp_transform_row())

        lines.extend([
            '    void parseFile(const std::string& path) {',
            '        rawRows.clear();',
        ])

        if self.file_ext == 'csv':
            lines.extend(self._generate_delimited_parser(','))
        elif self.file_ext == 'tsv':
            lines.extend(self._generate_delimited_parser('\\t'))
        elif self.file_ext == 'jsonl':
            lines.extend(self._generate_jsonl_parser())
        elif self.file_ext == 'json':
            lines.extend(self._generate_json_parser())

        lines.extend([
            '    }',
            '};',
            '',
        ])

        if has_neighbor_filters:
            lines.extend(self._generate_cpp_neighbor_filter_methods())

        lines.extend([
            'DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer)',
            '    : impl_(new Impl(buffer, "")) {}',
            '',
            'DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir)',
            '    : impl_(new Impl(buffer, cache_dir)) {}',
            '',
            'DynamicPreprocessor::~DynamicPreprocessor() { delete impl_; }',
            '',
            'void DynamicPreprocessor::open(const std::string& path) {',
            '    impl_->currentPath = path;',
            '    impl_->parseFile(path);',
            '    impl_->currentIdx = 0;',
            '    std::string cachePath = impl_->getCachePath(path);',
            '    impl_->loadCache(cachePath);',
        ])

        if has_stateful:
            lines.extend(self._generate_cpp_state_restore())

        lines.extend([
            '}',
            '',
            'bool DynamicPreprocessor::next(Row& out) {',
        ])

        if has_neighbor_filters:
            lines.extend(self._generate_cpp_next_with_neighbor_filters())
        else:
            lines.extend(self._generate_cpp_next_standard())

        lines.extend([
            f'}} // namespace {self.module_name}',
            '',
        ])

        return '\n'.join(lines)

    def _generate_delimited_parser(self, delim_char: str) -> List[str]:
        return [
            '        std::ifstream f(path);',
            '        if (!f.is_open()) return;',
            '        std::string line;',
            '        std::getline(f, line);',
            '        std::vector<std::string> headers;',
            '        std::stringstream hs(line);',
            '        std::string h;',
            f'        while (std::getline(hs, h, \'{delim_char}\')) headers.push_back(h);',
            '        while (std::getline(f, line)) {',
            '            if (line.empty()) continue;',
            '            std::map<std::string, std::string> row;',
            '            std::stringstream ls(line);',
            '            std::string val;',
            f'            for (size_t i = 0; i < headers.size() && std::getline(ls, val, \'{delim_char}\'); ++i) {{',
            '                row[headers[i]] = val;',
            '            }',
            '            rawRows.push_back(row);',
            '        }',
        ]

    def _generate_jsonl_parser(self) -> List[str]:
        return [
            '        std::ifstream f(path);',
            '        if (!f.is_open()) return;',
            '        std::string line;',
            '        while (std::getline(f, line)) {',
            '            if (line.empty()) continue;',
            '            // Simple JSONL parsing',
            '            std::map<std::string, std::string> row;',
            '            size_t pos = 0;',
            '            while ((pos = line.find("\\"", pos)) != std::string::npos) {',
            '                size_t keyStart = pos + 1;',
            '                size_t keyEnd = line.find("\\"", keyStart);',
            '                if (keyEnd == std::string::npos) break;',
            '                std::string key = line.substr(keyStart, keyEnd - keyStart);',
            '                size_t colon = line.find(":", keyEnd);',
            '                if (colon == std::string::npos) break;',
            '                size_t valStart = line.find_first_not_of(" \\t", colon + 1);',
            '                if (valStart == std::string::npos) break;',
            '                size_t valEnd;',
            '                if (line[valStart] == \'\\"\') {',
            '                    valEnd = line.find("\\"", valStart + 1);',
            '                    if (valEnd != std::string::npos) valEnd++;',
            '                } else if (line[valStart] == \'{\' || line[valStart] == \'[\') {',
            '                    int depth = 1;',
            '                    valEnd = valStart + 1;',
            '                    while (valEnd < line.size() && depth > 0) {',
            '                        if (line[valEnd] == \'{\' || line[valEnd] == \'[\') depth++;',
            '                        else if (line[valEnd] == \'}\' || line[valEnd] == \']\') depth--;',
            '                        valEnd++;',
            '                    }',
            '                } else {',
            '                    valEnd = line.find_first_of(",}]", valStart);',
            '                    if (valEnd == std::string::npos) valEnd = line.size();',
            '                }',
            '                std::string val = line.substr(valStart, valEnd - valStart);',
            '                // Remove quotes from string values',
            '                if (val.size() >= 2 && val.front() == \'\\"\' && val.back() == \'\\"\') {',
            '                    val = val.substr(1, val.size() - 2);',
            '                }',
            '                row[key] = val;',
            '                pos = valEnd;',
            '            }',
            '            rawRows.push_back(row);',
            '        }',
        ]

    def _generate_json_parser(self) -> List[str]:
        return [
            '        std::ifstream f(path);',
            '        if (!f.is_open()) return;',
            '        std::stringstream ss;',
            '        ss << f.rdbuf();',
            '        std::string content = ss.str();',
            '        // Simple JSON array parsing',
            '        size_t pos = content.find("[");',
            '        if (pos == std::string::npos) return;',
            '        pos++;',
            '        while (pos < content.size()) {',
            '            pos = content.find("{", pos);',
            '            if (pos == std::string::npos) break;',
            '            size_t objEnd = pos;',
            '            int depth = 1;',
            '            objEnd++;',
            '            while (objEnd < content.size() && depth > 0) {',
            '                if (content[objEnd] == \'{\' || content[objEnd] == \'[\') depth++;',
            '                else if (content[objEnd] == \'}\' || content[objEnd] == \']\') depth--;',
            '                objEnd++;',
            '            }',
            '            std::string obj = content.substr(pos, objEnd - pos);',
            '            std::map<std::string, std::string> row;',
            '            size_t keyPos = 0;',
            '            while ((keyPos = obj.find("\\"", keyPos)) != std::string::npos) {',
            '                size_t keyStart = keyPos + 1;',
            '                size_t keyEnd = obj.find("\\"", keyStart);',
            '                if (keyEnd == std::string::npos) break;',
            '                std::string key = obj.substr(keyStart, keyEnd - keyStart);',
            '                size_t colon = obj.find(":", keyEnd);',
            '                if (colon == std::string::npos) break;',
            '                size_t valStart = obj.find_first_not_of(" \\t", colon + 1);',
            '                if (valStart == std::string::npos) break;',
            '                size_t valEnd;',
            '                if (obj[valStart] == \'\\"\') {',
            '                    valEnd = obj.find("\\"", valStart + 1);',
            '                    if (valEnd != std::string::npos) valEnd++;',
            '                } else if (obj[valStart] == \'{\' || obj[valStart] == \'[\') {',
            '                    int d = 1;',
            '                    valEnd = valStart + 1;',
            '                    while (valEnd < obj.size() && d > 0) {',
            '                        if (obj[valEnd] == \'{\' || obj[valEnd] == \'[\') d++;',
            '                        else if (obj[valEnd] == \'}\' || obj[valEnd] == \']\') d--;',
            '                        valEnd++;',
            '                    }',
            '                } else {',
            '                    valEnd = obj.find_first_of(",}]", valStart);',
            '                    if (valEnd == std::string::npos) valEnd = obj.size();',
            '                }',
            '                std::string val = obj.substr(valStart, valEnd - valStart);',
            '                if (val.size() >= 2 && val.front() == \'\\"\' && val.back() == \'\\"\') {',
            '                    val = val.substr(1, val.size() - 2);',
            '                }',
            '                row[key] = val;',
            '                keyPos = valEnd;',
            '            }',
            '            rawRows.push_back(row);',
            '            pos = objEnd;',
            '        }',
        ]

    def _generate_cpp_state_vars(self) -> List[str]:
        lines = []
        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'    double prefixSum_{i} = 0.0;')
            elif st_type == 'prefix_count':
                lines.append(f'    long long prefixCount_{i} = 0;')
            elif st_type == 'sliding_window':
                window_size = st.get('window_size', 3)
                lines.append(f'    std::deque<double> window_{i};')
                lines.append(f'    size_t windowSize_{i} = {window_size};')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'    long long state_{i} = {initial};')
        return lines

    def _generate_cpp_state_load(self) -> List[str]:
        lines = []
        lines.append('        // Load state')
        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"prefix_sum_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) prefixSum_{i} = std::stod(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'prefix_count':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"prefix_count_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) prefixCount_{i} = std::stoll(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'sliding_window':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"window_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find("[", pos);',
                    f'                if (pos != std::string::npos) {{',
                    f'                    size_t end = content.find("]", pos);',
                    f'                    std::string arr = content.substr(pos + 1, end - pos - 1);',
                    f'                    std::stringstream ss(arr);',
                    f'                    std::string val;',
                    f'                    window_{i}.clear();',
                    f'                    while (std::getline(ss, val, \',\')) {{',
                    f'                        size_t start = val.find_first_not_of(" \\t\\n\\r");',
                    f'                        size_t end = val.find_last_not_of(" \\t\\n\\r");',
                    f'                        if (start != std::string::npos && end != std::string::npos) {{',
                    f'                            window_{i}.push_back(std::stod(val.substr(start, end - start + 1)));',
                    f'                        }}',
                    f'                    }}',
                    f'                }}',
                    f'            }}',
                    f'        }}',
                ])
            elif st_type == 'state_machine':
                lines.extend([
                    f'        {{',
                    f'            std::string key = "\\"state_{i}\\"";',
                    f'            size_t pos = content.find(key);',
                    f'            if (pos != std::string::npos) {{',
                    f'                pos = content.find(":", pos);',
                    f'                if (pos != std::string::npos) state_{i} = std::stoll(content.substr(pos + 1));',
                    f'            }}',
                    f'        }}',
                ])
        return lines

    def _generate_cpp_state_save(self) -> List[str]:
        lines = []
        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            if i > 0:
                lines.append('        f << ", ";')
            if st_type == 'prefix_sum':
                lines.append(f'        f << "\\"prefix_sum_{i}\\": " << prefixSum_{i};')
            elif st_type == 'prefix_count':
                lines.append(f'        f << "\\"prefix_count_{i}\\": " << prefixCount_{i};')
            elif st_type == 'sliding_window':
                lines.extend([
                    f'        f << "\\"window_{i}\\": [";',
                    f'        for (size_t j = 0; j < window_{i}.size(); ++j) {{',
                    f'            if (j > 0) f << ", ";',
                    f'            f << window_{i}[j];',
                    f'        }}',
                    f'        f << "]";',
                ])
            elif st_type == 'state_machine':
                lines.append(f'        f << "\\"state_{i}\\": " << state_{i};')
        return lines

    def _generate_cpp_state_restore(self) -> List[str]:
        lines = []
        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            if st_type == 'prefix_sum':
                lines.append(f'    impl_->prefixSum_{i} = 0.0;')
            elif st_type == 'prefix_count':
                lines.append(f'    impl_->prefixCount_{i} = 0;')
            elif st_type == 'sliding_window':
                lines.append(f'    impl_->window_{i}.clear();')
            elif st_type == 'state_machine':
                initial = st.get('initial_state', 1)
                lines.append(f'    impl_->state_{i} = {initial};')
        return lines

    def _generate_cpp_should_keep_row(self) -> List[str]:
        conditions = self.get_filter_conditions()
        lines = [
            '    bool shouldKeepRow(const std::map<std::string, std::string>& row) {',
        ]
        if not conditions:
            lines.append('        return true;')
        else:
            for cond in conditions:
                col = cond['column']
                op = cond['operator']
                val = cond['value']
                if op == '!=':
                    val_str = "true" if val else "false" if isinstance(val, bool) else str(val)
                    lines.append(f'        if (row.count("{col}") && row.at("{col}") == "{val_str}") return false;')
                elif op == '==':
                    val_str = "true" if val else "false" if isinstance(val, bool) else str(val)
                    lines.append(f'        if (!row.count("{col}") || row.at("{col}") != "{val_str}") return false;')
                elif op in ('>', '>=', '<', '<='):
                    comp_ops = {'>': '<=', '>=': '<', '<': '>=', '<=': '>'}
                    lines.append(f'        if (row.count("{col}") && !row.at("{col}").empty()) {{')
                    lines.append(f'            try {{')
                    lines.append(f'                double v = std::stod(row.at("{col}"));')
                    lines.append(f'                if (v {comp_ops[op]} {val}) return false;')
                    lines.append(f'            }} catch (...) {{}}')
                    lines.append(f'        }}')
            lines.append('        return true;')
        lines.append('    }')
        return lines

    def _generate_cpp_transform_row(self) -> List[str]:
        stateful_output_cols = self.get_stateful_output_columns()
        output_cols = self.get_output_columns()

        lines = [
            '    Row transformRow(const std::map<std::string, std::string>& rawRow) {',
            '        Row result;',
        ]

        for out_col in output_cols:
            if out_col in stateful_output_cols:
                continue

            transform = self.get_column_transforms().get(out_col, {'type': 'unknown'})
            t_type = transform.get('type')
            source = transform.get('source', out_col)

            if t_type in ('identity', 'copy'):
                lines.append(f'        if (rawRow.count("{source}")) result["{out_col}"] = parseValue(rawRow.at("{source}"));')
            elif t_type == 'constant':
                val = transform['value']
                if isinstance(val, bool):
                    lines.append(f'        result["{out_col}"] = Value({"true" if val else "false"});')
                elif isinstance(val, int):
                    lines.append(f'        result["{out_col}"] = Value({val}LL);')
                elif isinstance(val, float):
                    lines.append(f'        result["{out_col}"] = Value({val});')
                elif isinstance(val, str):
                    lines.append(f'        result["{out_col}"] = Value("{val}");')
                else:
                    lines.append(f'        result["{out_col}"] = Value();')
            elif t_type in ('strip', 'lower', 'upper', 'strip_lower', 'strip_upper'):
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                if 'strip' in t_type:
                    lines.append('            size_t start = s.find_first_not_of(" \\t\\n\\r");')
                    lines.append('            size_t end = s.find_last_not_of(" \\t\\n\\r");')
                    lines.append('            if (start != std::string::npos) s = s.substr(start, end - start + 1);')
                    lines.append('            else s.clear();')
                if t_type == 'lower' or t_type == 'strip_lower':
                    lines.append('            std::transform(s.begin(), s.end(), s.begin(), ::tolower);')
                elif t_type == 'upper' or t_type == 'strip_upper':
                    lines.append('            std::transform(s.begin(), s.end(), s.begin(), ::toupper);')
                lines.append(f'            result["{out_col}"] = Value(s);')
                lines.append('        }')
            elif t_type == 'add_prefix':
                prefix = transform['prefix']
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                lines.append(f'            result["{out_col}"] = Value("{prefix}" + s);')
                lines.append('        }')
            elif t_type == 'add_suffix':
                suffix = transform['suffix']
                lines.append(f'        if (rawRow.count("{source}")) {{')
                lines.append(f'            std::string s = rawRow.at("{source}");')
                lines.append(f'            result["{out_col}"] = Value(s + "{suffix}");')
                lines.append('        }')
            elif t_type == 'linear':
                a, b = transform['a'], transform['b']
                lines.append(f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{')
                lines.append(f'            try {{')
                lines.append(f'                double v = std::stod(rawRow.at("{source}"));')
                lines.append(f'                result["{out_col}"] = Value({a} * v + {b});')
                lines.append(f'            }} catch (...) {{}}')
                lines.append('        }')
            elif t_type != 'stateful':
                lines.append(f'        if (rawRow.count("{out_col}")) result["{out_col}"] = parseValue(rawRow.at("{out_col}"));')

        for i, st in enumerate(self.get_stateful_transforms()):
            st_type = st.get('type')
            out_col = st.get('output_column')
            source = st.get('source')
            a = st.get('a', 1.0)
            b = st.get('b', 0.0)

            if st_type == 'prefix_sum':
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{ prefixSum_{i} += std::stod(rawRow.at("{source}")); }} catch (...) {{}}',
                    '        }',
                    f'        result["{out_col}"] = Value({a} * prefixSum_{i} + {b});',
                ])
            elif st_type == 'prefix_count':
                condition = st.get('condition')
                if condition == 'not_null':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            prefixCount_{i}++;',
                        '        }',
                    ])
                elif condition == 'positive':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            try {{ if (std::stod(rawRow.at("{source}")) > 0) prefixCount_{i}++; }} catch (...) {{}}',
                        '        }',
                    ])
                elif condition == 'negative':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                        f'            try {{ if (std::stod(rawRow.at("{source}")) < 0) prefixCount_{i}++; }} catch (...) {{}}',
                        '        }',
                    ])
                elif condition == 'true':
                    lines.extend([
                        f'        if (rawRow.count("{source}") && rawRow.at("{source}") == "true") {{',
                        f'            prefixCount_{i}++;',
                        '        }',
                    ])
                else:
                    lines.append(f'        prefixCount_{i}++;')
                lines.append(f'        result["{out_col}"] = Value({a} * prefixCount_{i} + {b});')
            elif st_type == 'sliding_window':
                operation = st.get('operation', 'mean')
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{',
                    f'                window_{i}.push_back(std::stod(rawRow.at("{source}")));',
                    f'                while (window_{i}.size() > windowSize_{i}) window_{i}.pop_front();',
                    f'            }} catch (...) {{}}',
                    '        }',
                ])
                if operation == 'sum':
                    lines.extend([
                        f'        double windowVal_{i} = 0.0;',
                        f'        for (double v : window_{i}) windowVal_{i} += v;',
                    ])
                elif operation == 'mean':
                    lines.extend([
                        f'        double windowVal_{i} = 0.0;',
                        f'        for (double v : window_{i}) windowVal_{i} += v;',
                        f'        if (!window_{i}.empty()) windowVal_{i} /= window_{i}.size();',
                    ])
                lines.append(f'        result["{out_col}"] = Value({a} * windowVal_{i} + {b});')
            elif st_type == 'state_machine':
                transitions = st.get('transitions', [])
                lines.extend([
                    f'        if (rawRow.count("{source}") && !rawRow.at("{source}").empty()) {{',
                    f'            try {{',
                    f'                double val_sm = std::stod(rawRow.at("{source}"));',
                ])
                for t in transitions:
                    threshold = t.get('threshold')
                    direction = t.get('direction')
                    target_state = t.get('target_state')
                    if target_state is not None:
                        if direction == 'up':
                            lines.extend([
                                f'                if (val_sm >= {threshold} && state_{i} < {target_state}) state_{i} = {target_state};',
                            ])
                        else:
                            lines.extend([
                                f'                if (val_sm < {threshold} && state_{i} < {target_state}) state_{i} = {target_state};',
                            ])
                lines.extend([
                    '            } catch (...) {}',
                    '        }',
                    f'        result["{out_col}"] = Value(state_{i});',
                ])

        lines.append('        return result;')
        lines.append('    }')
        return lines

    def _generate_cpp_neighbor_filter_methods(self) -> List[str]:
        lines = []
        for i, nf in enumerate(self.get_neighbor_filters()):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                col = nf.get('column')
                val = nf.get('value')
                if isinstance(val, bool):
                    val_str_val = 'true' if val else 'false'
                elif isinstance(val, str):
                    val_str_val = val
                else:
                    val_str_val = str(val)
                lines.extend([
                    f'    bool checkNeighborFilter_{i}(const std::map<std::string, std::string>& current,',
                    f'                                const std::map<std::string, std::string>* next) {{',
                    f'        if (!next) return true;',
                    f'        if (next->count("{col}") && next->at("{col}") == "{val_str_val}") return false;',
                    '        return true;',
                    '    }',
                ])
            elif nf_type == 'consecutive_duplicate':
                col = nf.get('column')
                lines.extend([
                    f'    bool checkNeighborFilter_{i}(const std::map<std::string, std::string>& current,',
                    f'                                const std::map<std::string, std::string>* prev) {{',
                    f'        if (!prev) return true;',
                    f'        if (prev->count("{col}") && current.count("{col}") &&',
                    f'            prev->at("{col}") == current.at("{col}")) return false;',
                    '        return true;',
                    '    }',
                ])
        return lines

    def _generate_cpp_next_standard(self) -> List[str]:
        return [
            '    std::string cachePath = impl_->getCachePath(impl_->currentPath);',
            '    size_t bufferCount = 0;',
            '',
            '    while (impl_->currentIdx < impl_->rawRows.size()) {',
            '        if (impl_->currentIdx < impl_->processedRows) {',
            '            impl_->currentIdx++;',
            '            continue;',
            '        }',
            '',
            '        const auto& rawRow = impl_->rawRows[impl_->currentIdx];',
            '        impl_->currentIdx++;',
            '        impl_->processedRows = impl_->currentIdx;',
            '',
            '        if (!impl_->shouldKeepRow(rawRow)) {',
            '            impl_->saveCache(cachePath);',
            '            continue;',
            '        }',
            '',
            '        out = impl_->transformRow(rawRow);',
            '        impl_->cachedRows.push_back(out);',
            '        bufferCount++;',
            '',
            '        if (bufferCount >= impl_->buffer) {',
            '            impl_->saveCache(cachePath);',
            '            impl_->cachedRows.clear();',
            '            bufferCount = 0;',
            '        }',
            '',
            '        return true;',
            '    }',
            '',
            '    if (bufferCount > 0) {',
            '        impl_->saveCache(cachePath);',
            '        impl_->cachedRows.clear();',
            '    }',
            '',
            '    return false;',
            '}',
        ]

    def _generate_cpp_next_with_neighbor_filters(self) -> List[str]:
        neighbor_filters = self.get_neighbor_filters()
        lines = [
            '    std::string cachePath = impl_->getCachePath(impl_->currentPath);',
            '    size_t bufferCount = 0;',
            '',
            '    // Store pending row and prev row for neighbor filtering',
            '    std::map<std::string, std::string> pendingRow;',
            '    bool hasPending = false;',
            '    size_t pendingIdx = 0;',
            '    std::map<std::string, std::string> prevRow;',
            '    bool hasPrev = false;',
            '',
            '    // Replay to current state',
            '    for (size_t i = 0; i < impl_->currentIdx && i < impl_->rawRows.size(); ++i) {',
            '        if (hasPending && pendingIdx == i - 1) {',
            '            const auto& next = impl_->rawRows[i];',
            '            bool keep = true;',
        ]

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, &next);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '            if (keep && impl_->shouldKeepRow(pendingRow)) { /* row was kept */ }',
            '            hasPending = false;',
            '        }',
            '        prevRow = impl_->rawRows[i];',
            '        hasPrev = true;',
            '        pendingRow = impl_->rawRows[i];',
            '        hasPending = true;',
            '        pendingIdx = i;',
            '    }',
            '',
            '    // Continue processing',
            '    while (impl_->currentIdx < impl_->rawRows.size()) {',
            '        if (impl_->currentIdx < impl_->processedRows) {',
            '            impl_->currentIdx++;',
            '            continue;',
            '        }',
            '',
            '        const auto& rawRow = impl_->rawRows[impl_->currentIdx];',
            '',
            '        // Process pending row if exists',
            '        if (hasPending) {',
            '            bool keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, &rawRow);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'            keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '            if (keep && impl_->shouldKeepRow(pendingRow)) {',
            '                out = impl_->transformRow(pendingRow);',
            '                impl_->cachedRows.push_back(out);',
            '                bufferCount++;',
            '                impl_->processedRows = impl_->currentIdx;',
            '                impl_->saveCache(cachePath);',
            '',
            '                if (bufferCount >= impl_->buffer) {',
            '                    impl_->cachedRows.clear();',
            '                    bufferCount = 0;',
            '                }',
            '',
            '                pendingRow = rawRow;',
            '                pendingIdx = impl_->currentIdx;',
            '                prevRow = rawRow;',
            '                hasPrev = true;',
            '                impl_->currentIdx++;',
            '                hasPending = true;',
            '                return true;',
            '            }',
            '            hasPending = false;',
            '        }',
            '',
            '        // Set current as pending',
            '        pendingRow = rawRow;',
            '        pendingIdx = impl_->currentIdx;',
            '        prevRow = hasPrev ? prevRow : rawRow;',
            '        hasPrev = true;',
            '        hasPending = true;',
            '        impl_->currentIdx++;',
            '    }',
            '',
            '    // Handle last pending row',
            '    if (hasPending) {',
            '        bool keep = true;',
        ])

        for i, nf in enumerate(neighbor_filters):
            nf_type = nf.get('type')
            if nf_type == 'next_row_condition':
                lines.append(f'        keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, nullptr);')
            elif nf_type == 'consecutive_duplicate':
                lines.append(f'        keep = keep && impl_->checkNeighborFilter_{i}(pendingRow, hasPrev ? &prevRow : nullptr);')

        lines.extend([
            '        if (keep && impl_->shouldKeepRow(pendingRow)) {',
            '            out = impl_->transformRow(pendingRow);',
            '            impl_->cachedRows.push_back(out);',
            '            impl_->processedRows = impl_->currentIdx;',
            '            impl_->saveCache(cachePath);',
            '            hasPending = false;',
            '            return true;',
            '        }',
            '        hasPending = false;',
            '    }',
            '',
            '    if (bufferCount > 0) {',
            '        impl_->saveCache(cachePath);',
            '        impl_->cachedRows.clear();',
            '    }',
            '',
            '    return false;',
            '}',
        ])
        return lines
