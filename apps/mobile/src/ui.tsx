// iOS building blocks: semantic system colors (Dark Mode and Increase Contrast for free),
// Dynamic Type text styles, SF Symbols, and inset grouped lists like Settings and Reminders.
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { Children, isValidElement, useState, type ReactNode } from 'react';
import {
  DynamicColorIOS, PlatformColor, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View,
  type ColorValue, type TextInputProps, type TextProps,
} from 'react-native';

export const MIN_TOUCH = 44; // Apple HIG minimum target

export const color = {
  label: PlatformColor('label'),
  secondary: PlatformColor('secondaryLabel'),
  tertiary: PlatformColor('tertiaryLabel'),
  placeholder: PlatformColor('placeholderText'),
  ground: PlatformColor('systemGroupedBackground'),
  cell: PlatformColor('secondarySystemGroupedBackground'),
  fill: PlatformColor('tertiarySystemFill'),
  separator: PlatformColor('separator'),
  tint: PlatformColor('systemIndigo'),
  warning: PlatformColor('systemOrange'),
  danger: PlatformColor('systemRed'),
  neutral: PlatformColor('systemGray'),
  success: PlatformColor('systemGreen'),
  onTint: '#FFFFFF',
};

type Ramp = 'largeTitle' | 'title2' | 'title3' | 'headline' | 'body' | 'callout' | 'subheadline' | 'footnote' | 'caption1';
// Sizes and weights of the iOS text styles at the default reading size; dynamicTypeRamp scales them.
const RAMP: Record<Ramp, { fontSize: number; lineHeight: number; fontWeight?: '400' | '600' | '700' }> = {
  largeTitle: { fontSize: 34, lineHeight: 41, fontWeight: '700' },
  title2: { fontSize: 22, lineHeight: 28, fontWeight: '700' },
  title3: { fontSize: 20, lineHeight: 25, fontWeight: '600' },
  headline: { fontSize: 17, lineHeight: 22, fontWeight: '600' },
  body: { fontSize: 17, lineHeight: 22 },
  callout: { fontSize: 16, lineHeight: 21 },
  subheadline: { fontSize: 15, lineHeight: 20 },
  footnote: { fontSize: 13, lineHeight: 18 },
  caption1: { fontSize: 12, lineHeight: 16 },
};

export function T({ ramp = 'body', tone = color.label, style, ...rest }: TextProps & { ramp?: Ramp; tone?: ColorValue }) {
  return <Text dynamicTypeRamp={ramp} {...rest} style={[RAMP[ramp], { color: tone }, style]} />;
}

export function Icon({ name, size = 22, tint = color.tint, ...rest }: Omit<SymbolViewProps, 'tintColor'> & { tint?: ColorValue }) {
  return <SymbolView name={name} size={size} tintColor={tint} resizeMode="scaleAspectFit" style={{ width: size, height: size }} {...rest} />;
}

// The screen's first child must be its ScrollView: tab re-tap scrolls to top, large titles collapse.
export function Screen({ children, onRefresh }: { children: ReactNode; onRefresh?: () => Promise<unknown> }) {
  const [refreshing, setRefreshing] = useState(false);
  return (
    <ScrollView
      style={{ backgroundColor: color.ground }}
      contentInsetAdjustmentBehavior="automatic"
      contentContainerStyle={{ paddingHorizontal: 16, paddingTop: 8, paddingBottom: 40, gap: 28 }}
      keyboardShouldPersistTaps="handled"
      keyboardDismissMode="interactive"
      refreshControl={onRefresh && (
        <RefreshControl refreshing={refreshing} onRefresh={() => {
          setRefreshing(true);
          onRefresh().finally(() => setRefreshing(false));
        }} />
      )}
    >
      {children}
    </ScrollView>
  );
}

// An inset grouped section. Separators are drawn between rows, inset to the text column.
export function Group({ header, footer, children, inset = 16 }: { header?: string; footer?: ReactNode; children: ReactNode; inset?: number }) {
  const rows = Children.toArray(children).filter(isValidElement);
  return (
    <View>
      {header && <T ramp="footnote" tone={color.secondary} accessibilityRole="header" style={styles.groupHeader}>{header.toUpperCase()}</T>}
      {rows.length > 0 && (
        <View style={styles.group}>
          {rows.map((row, i) => (
            <View key={row.key ?? i}>
              {i > 0 && <View style={[styles.separator, { marginLeft: inset }]} />}
              {row}
            </View>
          ))}
        </View>
      )}
      {typeof footer === 'string' ? <T ramp="footnote" tone={color.secondary} style={styles.groupFooter}>{footer}</T> : footer}
    </View>
  );
}

export function Row({ children, onPress, accessibilityLabel, accessibilityHint, style }: {
  children: ReactNode; onPress?: () => void; accessibilityLabel?: string; accessibilityHint?: string; style?: object }) {
  const body = <View style={[styles.row, style]}>{children}</View>;
  if (!onPress) return body;
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={accessibilityLabel} accessibilityHint={accessibilityHint}
      style={({ pressed }) => pressed && { backgroundColor: color.fill }}>
      {body}
    </Pressable>
  );
}

// Settings-style "label ... value" row.
export function ValueRow({ label, value, tone }: { label: string; value: string; tone?: ColorValue }) {
  return (
    <Row>
      <T style={{ flex: 1 }}>{label}</T>
      <T tone={tone ?? color.secondary} style={{ fontVariant: ['tabular-nums'] }}>{value}</T>
    </Row>
  );
}

// A full-width tappable row that performs one action, like "Sign Out" in Settings.
export function ActionRow({ title, onPress, tone = color.tint, icon, disabled }: {
  title: string; onPress: () => void; tone?: ColorValue; icon?: SymbolViewProps['name']; disabled?: boolean }) {
  return (
    <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityState={{ disabled }}
      style={({ pressed }) => [pressed && { backgroundColor: color.fill }, disabled && { opacity: 0.4 }]}>
      <View style={styles.row}>
        {icon && <Icon name={icon} tint={tone} size={20} />}
        <T tone={tone}>{title}</T>
      </View>
    </Pressable>
  );
}

export function Field(props: TextInputProps) {
  return <TextInput placeholderTextColor={color.placeholder} clearButtonMode="while-editing" {...props}
    style={[styles.field, props.style]} />;
}

// Compact buttons inside a row. "filled" is the one primary action; "tinted" a secondary one.
export function Button({ title, onPress, icon, kind = 'plain', tone = color.tint, disabled, accessibilityLabel }: {
  title: string; onPress: () => void; icon?: SymbolViewProps['name']; kind?: 'filled' | 'tinted' | 'plain';
  tone?: ColorValue; disabled?: boolean; accessibilityLabel?: string }) {
  const filled = kind === 'filled';
  const fg = filled ? color.onTint : tone;
  return (
    <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityLabel={accessibilityLabel}
      accessibilityState={{ disabled }} hitSlop={kind === 'plain' ? 8 : 0}
      style={({ pressed }) => [
        styles.button,
        filled && { backgroundColor: tone },
        kind === 'tinted' && { backgroundColor: color.fill },
        kind === 'plain' && { paddingHorizontal: 0 },
        (pressed || disabled) && { opacity: disabled ? 0.4 : 0.6 },
      ]}>
      {icon && <Icon name={icon} tint={fg} size={15} weight="semibold" />}
      <T ramp="subheadline" tone={fg} style={{ fontWeight: '600' }}>{title}</T>
    </Pressable>
  );
}

export function Segmented<V extends string>({ options, value, onChange }: { options: { value: V; label: string }[]; value: V; onChange: (v: V) => void }) {
  return (
    <View style={styles.segments} accessibilityRole="tablist">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <Pressable key={o.value} onPress={() => onChange(o.value)} accessibilityRole="tab" accessibilityState={{ selected: on }}
            style={[styles.segment, on && styles.segmentOn]}>
            <T ramp="subheadline" style={{ fontWeight: on ? '600' : '400' }}>{o.label}</T>
          </Pressable>
        );
      })}
    </View>
  );
}

// Inline state attached to an item: queued, waiting for Windows, refused, conflicted.
export function Note({ icon, text, tone = color.warning }: { icon: SymbolViewProps['name']; text: string; tone?: ColorValue }) {
  return (
    <View style={{ flexDirection: 'row', gap: 6, alignItems: 'flex-start' }}>
      <Icon name={icon} tint={tone} size={14} style={{ width: 14, height: 14, marginTop: 2 }} />
      <T ramp="footnote" tone={tone} style={{ flex: 1 }}>{text}</T>
    </View>
  );
}

export function Empty({ icon, title, detail }: { icon: SymbolViewProps['name']; title: string; detail?: string }) {
  return (
    <View style={styles.empty}>
      <Icon name={icon} size={40} tint={color.tertiary} />
      <T ramp="headline" tone={color.secondary}>{title}</T>
      {detail && <T ramp="subheadline" tone={color.secondary} style={{ textAlign: 'center' }}>{detail}</T>}
    </View>
  );
}

export const styles = StyleSheet.create({
  groupHeader: { marginHorizontal: 16, marginBottom: 6 },
  groupFooter: { marginHorizontal: 16, marginTop: 6 },
  group: { backgroundColor: color.cell, borderRadius: 22, borderCurve: 'continuous', overflow: 'hidden' },
  separator: { height: StyleSheet.hairlineWidth, backgroundColor: color.separator },
  row: { minHeight: MIN_TOUCH, paddingHorizontal: 16, paddingVertical: 11, flexDirection: 'row', alignItems: 'center', gap: 12 },
  field: { flex: 1, minHeight: MIN_TOUCH, fontSize: 17, color: color.label },
  button: { minHeight: 34, paddingHorizontal: 14, borderRadius: 17, flexDirection: 'row', alignItems: 'center', gap: 5 },
  segments: { flexDirection: 'row', backgroundColor: color.fill, borderRadius: 10, padding: 2 },
  segment: { flex: 1, minHeight: 32, alignItems: 'center', justifyContent: 'center', borderRadius: 8 },
  // UISegmentedControl's thumb colours
  segmentOn: { backgroundColor: DynamicColorIOS({ light: '#FFFFFF', dark: '#636366' }), shadowColor: '#000', shadowOpacity: 0.12, shadowRadius: 4, shadowOffset: { width: 0, height: 1 } },
  empty: { alignItems: 'center', gap: 8, paddingVertical: 48, paddingHorizontal: 32 },
});
