import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from 'expo-audio';
import { useEffect } from 'react';
import { Pressable, View } from 'react-native';
import { color, Group, Icon, Row, T } from './ui';

// Synthetic speech generated with macOS `say`; no real recording.
const CLIP = require('../assets/synthetic/review-clip.m4a');

export function ClipCheck() {
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
    <Group header="Diagnostics" footer="Plays a synthetic clip to check review audio works with the ringer switch on silent.">
      <Row>
        <Pressable onPress={toggle} disabled={!status.isLoaded} accessibilityRole="button" hitSlop={8}
          accessibilityLabel={status.playing ? 'Pause test clip' : 'Play test clip'}>
          <Icon name={status.playing ? 'pause.circle.fill' : 'play.circle.fill'} size={30} />
        </Pressable>
        <View style={{ flex: 1 }}>
          <T>Review clip playback</T>
        </View>
        <T tone={color.secondary} style={{ fontVariant: ['tabular-nums'] }}>
          {status.isLoaded ? `${status.currentTime.toFixed(1)} / ${status.duration.toFixed(1)} s` : 'Loading…'}
        </T>
      </Row>
    </Group>
  );
}
