#include "dynamic_preprocessor.h"
#include <fstream>
#include <sstream>
#include <vector>
#include <nlohmann/json.hpp>
#include <filesystem>

namespace dynacpp {

namespace fs = std::filesystem;

// Forward declaration of impl
class DynamicPreprocessor::Impl {
public:
    Impl(std::size_t buffer, const std::optional<std::string>& cache_dir)
        : buffer_(buffer), cache_dir_(cache_dir), current_row_(0), eof_(false) {
        
        if (cache_dir_) {
            fs::create_directories(*cache_dir_);
        }
    }

    ~Impl() = default;

    void open(const std::string& path) {
        file_path_ = path;
        current_row_ = 0;
        eof_ = false;
        row_buffer_.clear();

        // Reset stateful transforms
        

        // Check for cache resume
        if (cache_dir_) {
            fs::path path_obj(path);
            std::string cache_key = path_obj.stem().string();
            std::replace(cache_key.begin(), cache_key.end(), '.', '_');

            cache_file_ = *cache_dir_ / (cache_key + "_cache.json");
            index_file_ = *cache_dir_ / (cache_key + "_index.txt");

            if (fs::exists(cache_file_) && fs::exists(index_file_)) {
                std::ifstream idx_f(index_file_);
                idx_f >> resume_from_;

                // Load cached rows
                std::ifstream cache_f(cache_file_);
                cache_f >> cached_rows_;
            }
        }
    }

    bool next(Row& out) {
        // Return cached rows first
        if (cached_row_index_ < cached_rows_.size()) {
            out = cached_rows_[cached_row_index_++];
            return true;
        }

        // Skip rows until resume point
        while (current_row_ < resume_from_) {
            std::map<std::string, Value> raw_row;
            if (!read_row(raw_row)) {
                eof_ = true;
                return false;
            }
            update_state(raw_row);
            current_row_++;
        }

        // Read and transform next row
        std::map<std::string, Value> raw_row;
        if (!read_row(raw_row)) {
            eof_ = true;
            return false;
        }

        // Apply transformation
        Row transformed = transform_row(raw_row);

        // Apply filter
        if ((row.at('id').!= Value{ValueType::Int, .int_value = 2} && row.at('age').!= Value{ValueType::Int, .int_value = 19} && row.at('name').!= Value{ValueType::String, .string_value = "Bob"})) {
            row_buffer_.push_back(transformed);

            if (row_buffer_.size() >= buffer_) {
                // Return buffered rows
                if (!row_buffer_.empty()) {
                    out = row_buffer_.front();
                    row_buffer_.pop_front();

                    // Update cache
                    if (cache_file_.empty() && cache_dir_) {
                        fs::path path_obj(file_path_);
                        std::string cache_key = path_obj.stem().string();
                        std::replace(cache_key.begin(), cache_key.end(), '.', '_');
                        cache_file_ = *cache_dir_ / (cache_key + "_cache.json");
                    }
                    if (!cache_file_.empty()) {
                        update_cache({out});
                    }
                    return true;
                }
            }
        }

        current_row_++;
        return false;
    }

private:
    Row transform_row(const std::map<std::string, Value>& row) {
        Row result;
        result.clear();
    result['id'] = row.at('id');
    result['name'] = row.at('name');
        return result;
    }

    bool read_row(std::map<std::string, Value>& row) {
                if (file_path_.endswith(".csv")) {
            std::ifstream f(file_path_);
            std::string line;
            if (std::getline(f, line)) {
                // Parse header
                headers_ = split(line, ',');
                while (std::getline(f, line)) {
                    if (line.empty()) continue;
                    auto values = split(line, ',');
                    for (size_t i = 0; i < std::min(headers_.size(), values.size()); i++) {
                        row[headers_[i]] = parse_value(values[i]);
                    }
                    return true;
                }
            }
        }
        return false;
    }

    void update_state(const std::map<std::string, Value>& row) {
        
    }

    void update_cache(const Row& row) {
        // Append to cache file
        std::ifstream f(cache_file_);
        nlohmann::json cached;
        if (f.is_open()) {
            f >> cached;
        }
        f.close();

        // Convert row to JSON
        nlohmann::json row_json;
        for (const auto& [k, v] : row) {
            row_json[k] = value_to_json(v);
        }
        cached.push_back(row_json);

        std::ofstream out(cache_file_);
        out << cached.dump(2);
    }

    nlohmann::json value_to_json(const Value& v) {
        switch (v.type) {
            case ValueType::Null: return nullptr;
            case ValueType::Bool: return v.bool_value;
            case ValueType::Int: return v.int_value;
            case ValueType::Double: return v.double_value;
            case ValueType::String: return v.string_value;
        }
        return nullptr;
    }

    std::size_t buffer_;
    std::optional<std::string> cache_dir_;
    std::string file_path_;
    fs::path cache_file_;
    fs::path index_file_;
    std::size_t current_row_;
    std::size_t resume_from_ = 0;
    bool eof_;
    std::deque<Row> row_buffer_;
    std::vector<Row> cached_rows_;
    std::size_t cached_row_index_ = 0;

    // Stateful transform state
    std::map<std::string, double> prefix_sums_;
    std::map<std::string, int> prefix_counts_;
    std::map<std::string, double> prefix_totals_;
    std::map<std::string, std::vector<double>> sliding_windows_;
    std::map<std::string, int> state_machines_;
};

// DynamicPreprocessor implementation
DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer)
    : impl_(new Impl(buffer, std::nullopt)) {
}

DynamicPreprocessor::DynamicPreprocessor(std::size_t buffer, const std::string& cache_dir)
    : impl_(new Impl(buffer, cache_dir)) {
}

void DynamicPreprocessor::open(const std::string& path) {
    impl_->open(path);
}

bool DynamicPreprocessor::next(Row& out) {
    return impl_->next(out);
}

} // namespace dynacpp
