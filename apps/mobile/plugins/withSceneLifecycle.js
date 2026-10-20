// iOS 27 UIKit traps at launch (EXC_BREAKPOINT in
// _UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption) unless the app
// adopts the UIScene life cycle. Expo SDK 57 ships ExpoAppSceneDelegate for this,
// but the default template does not wire it up, so this plugin does.
// ponytail: string patches on the SDK 57 template; delete once Expo's template adopts scenes.
const { withAppDelegate, withInfoPlist } = require('expo/config-plugins');

// `applied` detects a previous run so repeated prebuilds stay idempotent.
function replaceOnce(src, from, to, applied) {
  if (!src.includes(from) && applied(src)) return src;
  if (!src.includes(from)) {
    throw new Error(`withSceneLifecycle: AppDelegate template changed, could not find:\n${from}`);
  }
  return src.replace(from, to);
}

module.exports = function withSceneLifecycle(config) {
  config = withInfoPlist(config, (c) => {
    c.modResults.UIApplicationSceneManifest = {
      UIApplicationSupportsMultipleScenes: false,
      UISceneConfigurations: {
        UIWindowSceneSessionRoleApplication: [
          { UISceneConfigurationName: 'Default Configuration', UISceneDelegateClassName: 'EXExpoAppSceneDelegate' },
        ],
      },
    };
    return c;
  });
  return withAppDelegate(config, (c) => {
    if (c.modResults.language !== 'swift') throw new Error('withSceneLifecycle: expected a Swift AppDelegate');
    let src = c.modResults.contents;
    src = replaceOnce(src, 'class AppDelegate: ExpoAppDelegate {', 'class AppDelegate: ExpoAppDelegate, ExpoReactNativeFactoryProvider {', (s) => s.includes('ExpoReactNativeFactoryProvider {'));
    // The scene delegate creates the window and starts React Native into it.
    src = replaceOnce(
      src,
      `#if os(iOS) || os(tvOS)
    window = UIWindow(frame: UIScreen.main.bounds)
    factory.startReactNative(
      withModuleName: "main",
      in: window,
      launchOptions: launchOptions)
#endif
`,
      '',
      (s) => !s.includes('factory.startReactNative('),
    );
    c.modResults.contents = src;
    return c;
  });
};
