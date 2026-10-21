import { Stack } from 'expo-router';
import { color } from '../../ui';

// Large title that collapses into the bar on scroll, over the grouped background.
export default function TabStack() {
  return (
    <Stack screenOptions={{
      headerLargeTitleEnabled: true,
      headerTransparent: true,
      headerShadowVisible: false,
      headerLargeTitleShadowVisible: false,
      headerTintColor: color.tint,
      headerLargeStyle: { backgroundColor: 'transparent' },
    }} />
  );
}
