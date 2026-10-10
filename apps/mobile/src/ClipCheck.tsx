import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import { useEffect } from 'react';
import { Pressable, Text, View } from 'react-native';
import { MIN_TOUCH, useTheme } from './theme';

// Synthetic speech generated with macOS `say`; no real recording.
const CLIP = require('../assets/synthetic/review-clip.m4a');

export function ClipCheck() {
  const t = useTheme();
  const player = useAudioPlayer(CLIP);
  const status = useAudioPlayerStatus(player);

  useEffect(() => {
    // Review clips should be audible with the ringer off and pause other audio.
    setAudioModeAsync({ playsInSilentMode: true, interruptionMode: 'doNotMix' });
  }, []);

  const toggle = () => {
    if (status.playing) return player.pause();
    if (status.didJustFinish || status.currentTime >= status.duration) player.seekTo(0);
    player.play();
  };

  return (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
      <Pressable
        onPress={toggle}
        disabled={!status.isLoaded}
        accessibilityRole="button"
        accessibilityLabel={status.playing ? 'Pause clip' : 'Play clip'}
        style={{ minHeight: MIN_TOUCH, minWidth: 88, justifyContent: 'center', alignItems: 'center', borderRadius: 10, backgroundColor: t.accent }}
      >
        <Text style={{ color: '#FFFFFF', fontWeight: '600' }}>{status.playing ? 'Pause' : 'Play'}</Text>
      </Pressable>
      <Text style={{ color: t.muted }}>
        {status.isLoaded ? `${status.currentTime.toFixed(1)} / ${status.duration.toFixed(1)} s` : 'Loading…'}
      </Text>
    </View>
  );
}
