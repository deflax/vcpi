#pragma once

#include "beat_analysis.hpp"
#include <optional>
#include <string>

namespace vcpi {

class BeatAnalysisCache {
public:
    bool matches(const BeatAnalysisCache& other) const noexcept;
    explicit BeatAnalysisCache(const std::string& audioPath,
                               std::optional<double> bpmHint = std::nullopt) noexcept;
    std::optional<BeatAnalysis> load() const noexcept;
    void store(const BeatAnalysis& analysis) const noexcept;
    void erase() const;

private:
    std::optional<double> bpmHint;
    std::string sourcePath;
    std::string sourceIdentity;
    std::string directory;
    std::string filename;
};

} // namespace vcpi
