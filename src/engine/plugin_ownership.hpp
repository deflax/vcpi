#pragma once
#include "plugin.hpp"
#include <chrono>
#include <thread>

namespace vcpi {
// Stack-only guard shared by track rendering and the master-effects callback.
class PluginOwnership {
public:
    bool tryAcquire(Plugin* instrument, const std::vector<std::shared_ptr<Plugin>>& effects) noexcept {
        if (instrument && !acquireOne(instrument)) return false;
        for (const auto& effect : effects) if (!acquireOne(effect.get())) return false;
        return true;
    }
    void acquire(Plugin* instrument, const std::vector<std::shared_ptr<Plugin>>& effects) {
        while (!tryAcquire(instrument, effects)) {
            release();
            std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
    }
    ~PluginOwnership() { release(); }
private:
    bool acquireOne(Plugin* plugin) noexcept {
        if (!plugin->tryAcquire()) { release(); return false; }
        plugins[count++] = plugin;
        return true;
    }
    void release() noexcept { while (count) plugins[--count]->release(); }
    std::array<Plugin*, maxEffects + 1> plugins{};
    std::size_t count = 0;
};
} // namespace vcpi
