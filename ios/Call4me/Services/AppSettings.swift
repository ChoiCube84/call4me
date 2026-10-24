import Foundation

/// User preferences. Only non-private settings live here.
/// Never store `ask_user` answers or typed call text in UserDefaults.
final class AppSettings: ObservableObject {
    static let shared = AppSettings()

    static let defaultDisclosure =
        "Hi, I'm an AI assistant calling on behalf of {name}. They are Deaf and are reading this call live."

    static var defaultBackendURL: String {
        Bundle.main.object(forInfoDictionaryKey: "Call4meDefaultBackendURL") as? String
            ?? "http://localhost:8000"
    }

    private let store = UserDefaults.standard

    @Published var name: String { didSet { store.set(name, forKey: Keys.name) } }
    @Published var disclosureTemplate: String { didSet { store.set(disclosureTemplate, forKey: Keys.disclosure) } }
    @Published var backendURL: String { didSet { store.set(backendURL, forKey: Keys.backend) } }
    @Published var hasOnboarded: Bool { didSet { store.set(hasOnboarded, forKey: Keys.onboarded) } }

    /// Anonymous id sent as `user_id`. Created once per install.
    let userId: String

    private init() {
        name = store.string(forKey: Keys.name) ?? ""
        disclosureTemplate = store.string(forKey: Keys.disclosure) ?? Self.defaultDisclosure
        backendURL = store.string(forKey: Keys.backend) ?? Self.defaultBackendURL
        hasOnboarded = store.bool(forKey: Keys.onboarded)
        if let id = store.string(forKey: Keys.userId) {
            userId = id
        } else {
            let id = UUID().uuidString.lowercased()
            store.set(id, forKey: Keys.userId)
            userId = id
        }
    }

    /// Disclosure with the user's name filled in.
    var disclosureText: String {
        let shownName = name.trimmingCharacters(in: .whitespaces).isEmpty ? "my client" : name
        return disclosureTemplate.replacingOccurrences(of: "{name}", with: shownName)
    }

    var baseURL: URL? {
        URL(string: backendURL.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private enum Keys {
        static let name = "settings.name"
        static let disclosure = "settings.disclosureTemplate"
        static let backend = "settings.backendURL"
        static let onboarded = "settings.hasOnboarded"
        static let userId = "settings.userId"
    }
}
