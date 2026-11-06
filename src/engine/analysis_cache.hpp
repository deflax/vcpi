#pragma once

#include "beat_analysis.hpp"
#include <optional>
#include <string>

namespace vcpi {

class BeatAnalysisCache {
public:
    bool matches(const BeatAnalysisCache& other) const noexcept;
    explicit BeatAnalysisCache(const std::string& audioPath) noexcept;
    std::optional<BeatAnalysis> load() const noexcept;
    void store(const BeatAnalysis& analysis) const noexcept;
    void erase() const;

private:
    std::string sourcePath;
    std::string sourceIdentity;
    std::string directory;
    std::string filename;
};

} // namespace vcpi
