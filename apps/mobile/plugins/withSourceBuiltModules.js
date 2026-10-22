// Expo's precompiled module xcframeworks carry Swift interfaces from the compiler Expo
// built them with (6.3.1 for SDK 57); a newer Xcode (27.1, Swift 6.4) cannot import
// them. Build the modules from source with the local compiler instead.
// ponytail: costs a slower clean build; drop once Expo ships binaries matching Xcode.
const { withPodfileProperties } = require('expo/config-plugins');

module.exports = function withSourceBuiltModules(config) {
  return withPodfileProperties(config, (c) => {
    c.modResults.EXPO_USE_PRECOMPILED_MODULES = 'false';
    return c;
  });
};
