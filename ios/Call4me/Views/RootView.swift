import SwiftUI

/// Onboarding first, then the start screen. A live call covers the screen.
struct RootView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var router: AppRouter

    var body: some View {
        Group {
            if settings.hasOnboarded {
                NavigationStack { StartCallView() }
            } else {
                OnboardingView()
            }
        }
        .fullScreenCover(isPresented: callIsOpen) {
            if let id = router.activeCallId {
                LiveCallScreen(callId: id)
                    .environmentObject(settings)
                    .environmentObject(router)
            }
        }
    }

    private var callIsOpen: Binding<Bool> {
        Binding(
            get: { router.activeCallId != nil },
            set: { if !$0 { router.activeCallId = nil } }
        )
    }
}
