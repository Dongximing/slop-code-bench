#pragma once

#include <map>
#include <string>
#include <optional>
#include <cstddef>

namespace string_cpp {

enum class ValueType {
    Null,
    Bool,
    Int,
    Double,
    String
};

struct Value {
    ValueType type = ValueType::Null;
    bool bool_value = false;
    long long int_value = 0;
    double double_value = 0.0;
    std::string string_value;

    Value() : type(ValueType::Null) {}
    explicit Value(bool v) : type(ValueType::Bool), bool_value(v) {}
    explicit Value(long long v) : type(ValueType::Int), int_value(v) {}
    explicit Value(int v) : type(ValueType::Int), int_value(v) {}
    explicit Value(double v) : type(ValueType::Double), double_value(v) {}
    explicit Value(const std::string& v) : type(ValueType::String), string_value(v) {}
    explicit Value(const char* v) : type(ValueType::String), string_value(v) {}
};

using Row = std::map<std::string, Value>;

class DynamicPreprocessor {
public:
    explicit DynamicPreprocessor(std::size_t buffer);
    DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir);
    ~DynamicPreprocessor();

    void open(const std::string& path);
    bool next(Row& out);

private:
    struct Impl;
    Impl* impl_;
};

} // namespace string_cpp
