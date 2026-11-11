#include "stereo_sinc.hpp"
#include <juce_audio_basics/juce_audio_basics.h>

namespace vcpi {
const StereoSinc::Kernels& StereoSinc::sharedKernels() {
    // Build once on the deck construction thread, never on the render thread.
    // Obtain impulse responses from the pinned JUCE implementation instead of
    // maintaining a second window formula or a copied private coefficient table.
    static const auto table = [] {
        std::array<float, 10001> coefficients{};
        coefficients[0] = 1;
        std::array<float, history + 1> impulse{}, response{};
        impulse[0] = 1;
        for (int phase = 0; phase < phases; ++phase) {
            juce::WindowedSincInterpolator reference;
            float zero = 0, ignored = 0;
            const auto offset = phase == 0 ? 0.0 : 1.0 - static_cast<double>(phase) / phases;
            reference.process(offset, &zero, &ignored, 1);
            reference.process(1.0, impulse.data(), response.data(), history + 1);
            // Read the positive half, whose phase is calculated near zero.
            // Its 100 crossings recover a whole column of JUCE's lookup table
            // without the negative half's coarse float phase rounding.
            for (int crossing = 0; crossing < history / 2; ++crossing) {
                const auto at = phase == 0 ? (crossing + 1) * phases : crossing * phases + phase;
                coefficients[at] = response[history / 2 - crossing];
            }
        }
        Kernels result{};
        for (int phase = 0; phase <= phases; ++phase) {
            for (int tap = 0; tap < history / 2; ++tap) {
                result[phase][tap] = coefficients[(99 - tap) * phases + phase];
                result[phase][tap + history / 2] = coefficients[(tap + 1) * phases - phase];
            }
        }
        return result;
    }();
    return table;
}
StereoSinc::StereoSinc() : kernels(sharedKernels()) {}
void StereoSinc::reset(const float* left, const float* right, double fraction) noexcept {
    for (int channel = 0; channel < 2; ++channel) {
        const auto* input = channel == 0 ? left : right;
        std::copy_n(input, history, samples[channel].data());
        std::copy_n(input, history, samples[channel].data() + history);
    }
    index = 0;
    position = 1.0 + fraction;
}
} // namespace vcpi
