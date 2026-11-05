#pragma once
#include <atomic>
#include <cstdint>

namespace vcpi {
// A single bounded acquire attempt on the render side; only control threads wait.
class Ownership {
public:
    bool tryAcquire() noexcept {
        std::uint32_t expected = 0;
        return state.compare_exchange_strong(expected, 1, std::memory_order_acquire, std::memory_order_relaxed);
    }
    void acquire();
    void release() noexcept { state.store(0, std::memory_order_release); }
private:
    std::atomic<std::uint32_t> state{0};
};
class ControlOwnership {
public:
    explicit ControlOwnership(Ownership& value) : owner(value) { owner.acquire(); }
    ~ControlOwnership() { owner.release(); }
    ControlOwnership(const ControlOwnership&) = delete;
    ControlOwnership& operator=(const ControlOwnership&) = delete;
private:
    Ownership& owner;
};
} // namespace vcpi
