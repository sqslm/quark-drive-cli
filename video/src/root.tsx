import React from 'react';
import {Composition} from 'remotion';
import {QuarkDriveIntro} from './video';

export const Root: React.FC = () => (
  <Composition
    id="QuarkDriveIntro"
    component={QuarkDriveIntro}
    durationInFrames={990}
    fps={30}
    width={1280}
    height={720}
  />
);
