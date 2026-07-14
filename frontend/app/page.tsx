'use client';

import { useMemo } from 'react';
import dynamic from 'next/dynamic';
import './tldraw.css';

import { Vibe3DCodeButton } from './components/Vibe3DCodeButton';
import { AutoDrawButton } from './components/AutoDrawButton';
import { ImproveDrawingButton } from './components/ImproveDrawingButton';

import { PreviewShapeUtil } from './PreviewShape/PreviewShape';
import { Model3DPreviewShapeUtil } from './PreviewShape/Model3DPreviewShape';

import { useTabStore } from './store/appStore';
import TestAddCodeButton from './components/TestAddCodeButton';
import { TldrawLogo } from './components/TldrawLogo';

import {
	createTLStore,
	defaultShapeUtils,
} from '@tldraw/tldraw';

// ---------- Dynamic imports ----------

const Tldraw = dynamic(
	() => import('@tldraw/tldraw').then((m) => m.Tldraw),
	{
		ssr: false,
	}
);

const ThreeJSCanvas = dynamic(
	() => import('./components/three/canvas'),
	{
		ssr: false,
		loading: () => (
			<div
				style={{
					width: '100%',
					height: '100%',
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
					background: '#111',
					color: 'white',
				}}
			>
				Loading 3D...
			</div>
		),
	}
);

// ---------- Shapes ----------

const shapeUtils = [
	PreviewShapeUtil,
	Model3DPreviewShapeUtil,
];

type TabType = 'tldraw' | 'threejs';

interface TabGroupProps {
	activeTab: TabType;
	setActiveTab: (tab: TabType) => void;
}

function TabGroup({
					  activeTab,
					  setActiveTab,
				  }: TabGroupProps) {
	return (
		<div
			style={{
				position: 'fixed',
				top: 20,
				left: '50%',
				transform: 'translateX(-50%)',
				zIndex: 9999999,
				display: 'flex',
				gap: 6,
				padding: 6,
				borderRadius: 8,
				background: 'white',
				boxShadow: '0 4px 12px rgba(0,0,0,.15)',
			}}
		>
			<button
				onClick={() => setActiveTab('tldraw')}
				style={{
					padding: '6px 12px',
					border: 'none',
					borderRadius: 4,
					cursor: 'pointer',
					background:
						activeTab === 'tldraw'
							? '#007bff'
							: '#f0f0f0',
					color:
						activeTab === 'tldraw'
							? 'white'
							: 'black',
				}}
			>
				2D Canvas
			</button>

			<button
				onClick={() => setActiveTab('threejs')}
				style={{
					padding: '6px 12px',
					border: 'none',
					borderRadius: 4,
					cursor: 'pointer',
					background:
						activeTab === 'threejs'
							? '#007bff'
							: '#f0f0f0',
					color:
						activeTab === 'threejs'
							? 'white'
							: 'black',
				}}
			>
				3D World
			</button>
		</div>
	);
}

// Унікальний ключ для автозбереження. Якщо колись зміните структуру
// власних shape-утилів (PreviewShape, Model3DPreviewShape) так, що старі
// збережені малюнки стануть несумісні — просто змініть цей рядок,
// і tldraw почне зберігати стан "з чистого аркуша" під новим ключем.
const PERSISTENCE_KEY = 'vibe-draw-autosave-v1';

function MainEditor() {
	const { activeTab, setActiveTab } = useTabStore();

	const customStore = useMemo(() => {
		const store = createTLStore({
			shapeUtils: [...defaultShapeUtils, ...shapeUtils],
		});

		if (typeof window !== 'undefined') {
			// 1. Спробуємо завантажити збережений стан
			try {
				const raw = localStorage.getItem(PERSISTENCE_KEY);
				if (raw) {
					const snapshot = JSON.parse(raw);
					store.loadSnapshot(snapshot);
					console.log('[vibe-draw] Малюнок відновлено з localStorage');
				} else {
					console.log('[vibe-draw] Збереженого малюнка не знайдено — починаємо з чистого аркуша');
				}
			} catch (e) {
				console.error('[vibe-draw] Не вдалося завантажити збережений малюнок:', e);
			}

			// 2. Підписуємося на оновлення. Фільтруємо тільки зміни документа
			// від користувача (ігноруємо ephemeral-стан на кшталт виділення,
			// позиції камери, наведення курсору тощо), щоб не засмічувати
			// localStorage і не ловити зайвих ре-рендерів.
			store.listen(
				() => {
					try {
						const snapshot = store.getSnapshot();
						localStorage.setItem(PERSISTENCE_KEY, JSON.stringify(snapshot));
					} catch (e) {
						console.error('[vibe-draw] Не вдалося зберегти малюнок:', e);
					}
				},
				{ source: 'user', scope: 'document' }
			);
		}

		return store;
	}, []);

	return (
		<>
			<TabGroup
				activeTab={activeTab}
				setActiveTab={setActiveTab}
			/>

			<div className="editor">
				<div
					style={{
						position: 'absolute',
						width: '100%',
						height: '100%',
						visibility:
							activeTab === 'tldraw'
								? 'visible'
								: 'hidden',
						zIndex:
							activeTab === 'tldraw'
								? 2
								: 1,
					}}
				>
					<Tldraw
						store={customStore}
						shapeUtils={shapeUtils}
						shareZone={
							<div style={{ display: 'flex' }}>
								<Vibe3DCodeButton />
								<ImproveDrawingButton />
								<AutoDrawButton />
							</div>
						}
					>
						<TldrawLogo />
					</Tldraw>
				</div>

				<ThreeJSCanvas visible={activeTab === 'threejs'} />
			</div>

			<TestAddCodeButton
				activeTab={activeTab}
				setActiveTab={setActiveTab}
			/>
		</>
	);
}

const DynamicApp = dynamic(
	() => Promise.resolve(MainEditor),
	{
		ssr: false,
		loading: () => (
			<div
				style={{
					height: '100vh',
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
				}}
			>
				Loading Editor...
			</div>
		),
	}
);

export default function Page() {
	return <DynamicApp />
}
