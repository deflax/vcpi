#include "analysis_cache.hpp"
#include <juce_core/juce_core.h>
#include <array>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace vcpi {
namespace {
constexpr std::size_t maximumEntryBytes = 16384;

struct Descriptor {
    explicit Descriptor(int value) noexcept : value(value) {}
    ~Descriptor() { if (value >= 0) ::close(value); }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    int value;
};

struct SourceIdentity {
    std::string path;
    std::string json;
};

std::optional<SourceIdentity> identify(const std::string& path) {
    std::error_code error;
    const auto canonical = std::filesystem::canonical(path, error);
    if (error) return std::nullopt;
    struct stat status {};
    if (::stat(canonical.c_str(), &status) != 0 || !S_ISREG(status.st_mode) || status.st_size < 0)
        return std::nullopt;
    auto* identity = new juce::DynamicObject;
    const juce::var object(identity);
    identity->setProperty("path", juce::String::fromUTF8(canonical.c_str()));
    identity->setProperty("size", static_cast<juce::int64>(status.st_size));
    // Strings preserve the full unsigned inode/device range in inspectable JSON.
    identity->setProperty("device", juce::String(std::to_string(static_cast<std::uint64_t>(status.st_dev))));
    identity->setProperty("inode", juce::String(std::to_string(static_cast<std::uint64_t>(status.st_ino))));
   #if defined(__APPLE__)
    const auto modified = status.st_mtimespec;
    const auto changed = status.st_ctimespec;
   #else
    const auto modified = status.st_mtim;
    const auto changed = status.st_ctim;
   #endif
    identity->setProperty("mtime_seconds", static_cast<juce::int64>(modified.tv_sec));
    identity->setProperty("mtime_nanoseconds", static_cast<juce::int64>(modified.tv_nsec));
    identity->setProperty("ctime_seconds", static_cast<juce::int64>(changed.tv_sec));
    identity->setProperty("ctime_nanoseconds", static_cast<juce::int64>(changed.tv_nsec));
    identity->setProperty("analyzer_version", beatAnalyzerVersion);
    return SourceIdentity{canonical.string(), juce::JSON::toString(object, true).toStdString()};
}

std::string cacheDirectory() {
    std::filesystem::path base;
    const auto* override = std::getenv("XDG_CACHE_HOME");
    if (override != nullptr && *override != '\0' && std::filesystem::path(override).is_absolute())
        base = override;
    else {
        const auto* home = std::getenv("HOME");
        if (home != nullptr && *home != '\0' && std::filesystem::path(home).is_absolute())
            base = home;
        else
            base = juce::File::getSpecialLocation(juce::File::userHomeDirectory).getFullPathName().toStdString();
        if (!base.is_absolute()) return {};
       #if defined(__APPLE__)
        base /= "Library/Caches";
       #else
        base /= ".cache";
       #endif
    }
    return (base / "vcpi" / "analysis").string();
}

std::string cacheFilename(const std::string& path) {
    // The embedded identity is authoritative, so even a hash collision is a miss,
    // never a grid belonging to another song. One stable slot per canonical path.
    std::uint64_t hash = UINT64_C(14695981039346656037);
    for (const unsigned char byte : path) {
        hash ^= byte;
        hash *= UINT64_C(1099511628211);
    }
    std::array<char, 22> name {};
    std::snprintf(name.data(), name.size(), "%016llx.json", static_cast<unsigned long long>(hash));
    return name.data();
}

bool privateDirectory(const std::filesystem::path& path) {
    if (::mkdir(path.c_str(), 0700) != 0 && errno != EEXIST) return false;
    struct stat status {};
    return ::lstat(path.c_str(), &status) == 0 && S_ISDIR(status.st_mode) &&
           status.st_uid == ::geteuid() && (status.st_mode & 0022) == 0;
}

bool prepareDirectory(const std::string& directory) {
    const std::filesystem::path path(directory);
    std::error_code error;
    std::filesystem::create_directories(path.parent_path().parent_path(), error);
    return !error && privateDirectory(path.parent_path()) && privateDirectory(path);
}

bool numeric(const juce::var& value) {
    return value.isDouble() || value.isInt() || value.isInt64();
}

bool validAnalysis(const BeatAnalysis& analysis) {
    if (!std::isfinite(analysis.bpm) || !std::isfinite(analysis.offset) ||
        !std::isfinite(analysis.confidence)) return false;
    if (analysis.bpm == 0)
        return analysis.offset == 0 && analysis.confidence == 0;
    return analysis.bpm >= 20 && analysis.bpm <= 400 && analysis.offset >= 0 &&
           analysis.offset < 60 / analysis.bpm && analysis.confidence > 0 && analysis.confidence <= 1;
}

bool sameIdentity(const juce::var& saved, const std::string& identity) {
    const auto expected = juce::JSON::parse(juce::String(identity));
    const auto* actualObject = saved.getDynamicObject();
    const auto* expectedObject = expected.getDynamicObject();
    if (actualObject == nullptr || expectedObject == nullptr ||
        actualObject->getProperties().size() != expectedObject->getProperties().size()) return false;
    for (const auto& property : expectedObject->getProperties()) {
        const auto& actual = actualObject->getProperty(property.name);
        if (property.value.isString()) {
            if (!actual.isString() || actual != property.value) return false;
        } else {
            if ((!actual.isInt() && !actual.isInt64()) ||
                static_cast<juce::int64>(actual) != static_cast<juce::int64>(property.value)) return false;
        }
    }
    return true;
}
}

BeatAnalysisCache::BeatAnalysisCache(const std::string& audioPath) noexcept {
    try {
        sourcePath = audioPath;
        const auto identity = identify(sourcePath);
        if (!identity) return;
        sourceIdentity = identity->json;
        directory = cacheDirectory();
        filename = cacheFilename(identity->path);
    } catch (...) {
        directory.clear();
        filename.clear();
    }
}

bool BeatAnalysisCache::matches(const BeatAnalysisCache& other) const noexcept {
    return !sourceIdentity.empty() && sourceIdentity == other.sourceIdentity;
}

std::optional<BeatAnalysis> BeatAnalysisCache::load() const noexcept {
    try {
        if (sourceIdentity.empty() || directory.empty() || filename.empty()) return std::nullopt;
        const auto current = identify(sourcePath);
        if (!current || current->json != sourceIdentity) return std::nullopt;
        const Descriptor folder(::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (folder.value < 0) return std::nullopt;
        const Descriptor file(::openat(folder.value, filename.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
        if (file.value < 0) return std::nullopt;
        struct stat status {};
        if (::fstat(file.value, &status) != 0 || !S_ISREG(status.st_mode) || status.st_uid != ::geteuid() ||
            status.st_size <= 0 || status.st_size > static_cast<off_t>(maximumEntryBytes)) return std::nullopt;
        std::array<char, maximumEntryBytes + 1> buffer {};
        std::size_t size = 0;
        while (size < buffer.size()) {
            const auto count = ::read(file.value, buffer.data() + size, buffer.size() - size);
            if (count < 0) {
                if (errno == EINTR) continue;
                return std::nullopt;
            }
            if (count == 0) break;
            size += static_cast<std::size_t>(count);
        }
        if (size == 0 || size > maximumEntryBytes) return std::nullopt;
        const auto entry = juce::JSON::parse(juce::String::fromUTF8(buffer.data(), static_cast<int>(size)));
        if (!entry.isObject() || !entry["schema"].isInt() || static_cast<int>(entry["schema"]) != 1 ||
            !sameIdentity(entry["source"], sourceIdentity) || !numeric(entry["bpm"]) ||
            !numeric(entry["offset"]) || !numeric(entry["confidence"])) return std::nullopt;
        const BeatAnalysis analysis{static_cast<double>(entry["bpm"]), static_cast<double>(entry["offset"]),
                                    static_cast<double>(entry["confidence"])};
        if (!validAnalysis(analysis)) return std::nullopt;
        const auto after = identify(sourcePath);
        if (!after || after->json != sourceIdentity) return std::nullopt;
        return analysis;
    } catch (...) {
        return std::nullopt;
    }
}

void BeatAnalysisCache::store(const BeatAnalysis& analysis) const noexcept {
    try {
        if (sourceIdentity.empty() || directory.empty() || filename.empty() || !validAnalysis(analysis)) return;
        const auto current = identify(sourcePath);
        if (!current || current->json != sourceIdentity || !prepareDirectory(directory)) return;
        auto* object = new juce::DynamicObject;
        const juce::var entry(object);
        object->setProperty("schema", 1);
        object->setProperty("source", juce::JSON::parse(juce::String(sourceIdentity)));
        object->setProperty("bpm", analysis.bpm);
        object->setProperty("offset", analysis.offset);
        object->setProperty("confidence", analysis.confidence);
        const auto json = juce::JSON::toString(entry, true, 17).toStdString();
        if (json.size() > maximumEntryBytes) return;
        const Descriptor folder(::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
        if (folder.value < 0) return;
        static std::atomic<std::uint64_t> serial {0};
        for (int attempt = 0; attempt < 8; ++attempt) {
            const auto temporary = "." + filename + "." + std::to_string(::getpid()) + "." +
                                   std::to_string(serial.fetch_add(1, std::memory_order_relaxed));
            const Descriptor file(::openat(folder.value, temporary.c_str(),
                                            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600));
            if (file.value < 0) {
                if (errno == EEXIST) continue;
                return;
            }
            struct RemoveTemporary {
                int folder;
                const std::string& name;
                ~RemoveTemporary() { ::unlinkat(folder, name.c_str(), 0); }
            } cleanup {folder.value, temporary};
            std::size_t written = 0;
            while (written < json.size()) {
                const auto count = ::write(file.value, json.data() + written, json.size() - written);
                if (count < 0 && errno == EINTR) continue;
                if (count <= 0) return;
                written += static_cast<std::size_t>(count);
            }
            if (::fsync(file.value) != 0) return;
            const auto after = identify(sourcePath);
            if (!after || after->json != sourceIdentity) return;
            if (::renameat(folder.value, temporary.c_str(), folder.value, filename.c_str()) == 0)
                ::fsync(folder.value);
            return;
        }
    } catch (...) {
        // Analysis caching is optional; an unavailable cache must not stop playback.
    }
}

} // namespace vcpi
