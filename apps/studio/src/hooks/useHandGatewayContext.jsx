import React from 'react';
import { useHandGateway } from './useHandGateway';

// One hand-gateway websocket per app, shared by everything that talks to :9003.
//
// Before this, each component that wanted the hand called useHandGateway() itself, which was
// harmless only because one page was mounted at a time. The single-page layout puts the
// connection controls and the hand controls on screen together, and two hook instances would
// mean two sockets, two heartbeats, and a watchdog fed by a connection the operator did not
// think they were using.
const HandGatewayContext = React.createContext(null);

export function HandGatewayProvider({ children, value }) {
  // `value` is an injection point for tests; the app passes nothing and gets the real hook.
  const owned = useHandGateway();
  return React.createElement(HandGatewayContext.Provider, { value: value ?? owned }, children);
}

export function useHandGatewayContext() {
  const ctx = React.useContext(HandGatewayContext);
  if (!ctx) throw new Error('useHandGatewayContext must be used inside <HandGatewayProvider>');
  return ctx;
}
