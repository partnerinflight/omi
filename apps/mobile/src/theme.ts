import { useColorScheme } from 'react-native';

const light = { bg: '#F7F7F5', card: '#FFFFFF', text: '#1C1C1E', muted: '#5F5F66', accent: '#0A66D1', border: '#D8D8DC', badge: '#8A5A00' };
const dark: typeof light = { bg: '#000000', card: '#1C1C1E', text: '#F2F2F7', muted: '#A1A1A8', accent: '#4DA3FF', border: '#38383C', badge: '#FFC24D' };

export const MIN_TOUCH = 44; // Apple HIG minimum target

export function useTheme() {
  return useColorScheme() === 'dark' ? dark : light;
}
