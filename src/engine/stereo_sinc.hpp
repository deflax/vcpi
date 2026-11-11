#pragma once
#include <algorithm>
#include <array>

namespace vcpi {
// The same 200-sample JUCE sinc kernel for both channels, with a shared phase.
// A mirrored history makes each dot product contiguous, including at wraparound.
class StereoSinc {
public:
    static constexpr int history = 200;
    StereoSinc();
    void reset(const float* left, const float* right, double fraction) noexcept;

    int process(double ratio, const float* left, const float* right,
                float& outputLeft, float& outputRight) noexcept {
        int used = 0;
        while (position >= 1.0) {
            samples[0][index] = samples[0][index + history] = left[used];
            samples[1][index] = samples[1][index + history] = right[used];
            if (++index == history) index = 0;
            ++used;
            position -= 1.0;
        }
        const auto* inputLeft = samples[0].data() + index;
        const auto* inputRight = samples[1].data() + index;
        // Independent lanes permit SIMD without fast-math or a serial reduction
        // inside the tap loop. Coefficients are calculated once for stereo.
        std::array<float, 4> sumLeft{}, sumRight{};
        const auto accumulate = [&](int begin, int lower, float fraction) {
            const auto* first = kernels[lower].data();
            const auto* next = kernels[lower + 1].data();
            for (int tap = begin; tap < begin + history / 2; tap += 4) {
                for (int lane = 0; lane < 4; ++lane) {
                    const auto at = tap + lane;
                    const auto coefficient = first[at] + fraction * (next[at] - first[at]);
                    sumLeft[lane] += inputLeft[at] * coefficient;
                    sumRight[lane] += inputRight[at] * coefficient;
                }
            }
        };
        // Match JUCE's separate float lookup phases on either side of zero.
        // In particular, calculating (99 + offset) loses more float precision
        // than (1 - offset); using one ideal phase for both changes the filter.
        const auto offset = static_cast<float>(position);
        const auto negative = -((1.0f - offset) - 100.0f) * phases;
        const auto lower = std::min(static_cast<int>(negative) - 9900, phases - 1);
        accumulate(0, lower, negative - (9900 + lower));
        const auto positive = (1.0f - offset) * phases;
        const auto integral = static_cast<int>(positive);
        if (integral == phases) accumulate(history / 2, 0, 0);
        else accumulate(history / 2, phases - 1 - integral, 1.0f - (positive - integral));
        outputLeft = (sumLeft[0] + sumLeft[1]) + (sumLeft[2] + sumLeft[3]);
        outputRight = (sumRight[0] + sumRight[1]) + (sumRight[2] + sumRight[3]);
        position += ratio;
        return used;
    }

private:
    static constexpr int phases = 100;
    using Kernels = std::array<std::array<float, history>, phases + 1>;
    static const Kernels& sharedKernels();
    const Kernels& kernels;
    std::array<std::array<float, history * 2>, 2> samples{};
    double position = 1.0;
    int index = 0;
};
} // namespace vcpi
