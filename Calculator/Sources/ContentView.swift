
import SwiftUI

struct ContentView: View {
    @State private var currentValue = "0"
    @State private var runningSum = 0.0
    @State private var currentOperation: Operation = .none
    @State private var shouldClearDisplay = false

    enum Operation {
        case add, subtract, divide, none
    }

    let buttons: [[CalculatorButton]] = [
        [.seven, .eight, .nine, .divide],
        [.four, .five, .six, .subtract],
        [.one, .two, .three, .add],
        [.zero, .clear, .equals]
    ]

    var body: some View {
        ZStack {
            // Modern gradient background
            LinearGradient(
                gradient: Gradient(colors: [
                    Color(red: 0.1, green: 0.1, blue: 0.12),
                    Color(red: 0.15, green: 0.15, blue: 0.18)
                ]),
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .edgesIgnoringSafeArea(.all)

            VStack(spacing: 0) {
                // Header with title
                VStack(spacing: 8) {
                    Text("Calculator")
                        .font(.system(size: 28, weight: .semibold, design: .rounded))
                        .foregroundColor(.white.opacity(0.7))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 24)
                .padding(.top, 20)
                .padding(.bottom, 16)

                Spacer()

                // Display
                VStack(alignment: .trailing, spacing: 8) {
                    Text(currentValue)
                        .font(.system(size: 60, weight: .light, design: .default))
                        .foregroundColor(.white)
                        .lineLimit(1)
                        .minimumScaleFactor(0.5)
                }
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.horizontal, 24)
                .padding(.vertical, 32)
                .background(
                    RoundedRectangle(cornerRadius: 20)
                        .fill(Color(red: 0.2, green: 0.2, blue: 0.22))
                )
                .padding(.horizontal, 16)
                .padding(.bottom, 32)

                // Buttons
                VStack(spacing: 16) {
                    ForEach(buttons, id: \.self) { row in
                        HStack(spacing: 16) {
                            ForEach(row, id: \.self) { button in
                                Button(action: {
                                    self.didTap(button: button)
                                }) {
                                    Text(button.rawValue)
                                        .font(.system(size: 24, weight: .semibold, design: .default))
                                        .frame(
                                            maxWidth: .infinity,
                                            maxHeight: .infinity
                                        )
                                        .foregroundColor(.white)
                                        .background(button.buttonColor)
                                        .cornerRadius(16)
                                        .shadow(color: Color.black.opacity(0.3), radius: 8, x: 0, y: 4)
                                }
                                .frame(height: 70)
                            }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 32)
            }
        }
    }

    func didTap(button: CalculatorButton) {
        switch button {
        case .add, .subtract, .divide:
            if let value = Double(currentValue) {
                let selectedOperation: Operation

                switch button {
                case .add:
                    selectedOperation = .add
                case .subtract:
                    selectedOperation = .subtract
                case .divide:
                    selectedOperation = .divide
                default:
                    selectedOperation = .none
                }

                if shouldClearDisplay {
                    currentOperation = selectedOperation
                } else {
                    if currentOperation == .none {
                        runningSum = value
                    } else {
                        guard let result = perform(operation: currentOperation, lhs: runningSum, rhs: value) else {
                            showError()
                            return
                        }

                        runningSum = result
                        currentValue = "\(runningSum)"
                    }

                    currentOperation = selectedOperation
                    shouldClearDisplay = true
                }
            }
        case .equals:
            if let value = Double(currentValue) {
                let result: Double

                switch currentOperation {
                case .none:
                    result = value
                default:
                    guard let computedResult = perform(operation: currentOperation, lhs: runningSum, rhs: value) else {
                        showError()
                        return
                    }

                    result = computedResult
                }

                currentValue = "\(result)"
                runningSum = result
                currentOperation = .none
                shouldClearDisplay = true
            }
        case .clear:
            currentValue = "0"
            runningSum = 0
            currentOperation = .none
            shouldClearDisplay = false
        default:
            let number = button.rawValue
            if shouldClearDisplay {
                currentValue = number
                shouldClearDisplay = false
            } else {
                currentValue = currentValue == "0" ? number : currentValue + number
            }
        }
    }

    private func perform(operation: Operation, lhs: Double, rhs: Double) -> Double? {
        switch operation {
        case .add:
            return lhs + rhs
        case .subtract:
            return lhs - rhs
        case .divide:
            guard rhs != 0 else {
                return nil
            }

            return lhs / rhs
        case .none:
            return rhs
        }
    }

    private func showError() {
        currentValue = "Error"
        runningSum = 0
        currentOperation = .none
        shouldClearDisplay = true
    }
}

enum CalculatorButton: String {
    case zero = "0", one = "1", two = "2", three = "3", four = "4", five = "5", six = "6", seven = "7", eight = "8", nine = "9"
    case equals = "=", add = "+", subtract = "-", divide = "÷"
    case clear = "AC"

    var buttonColor: Color {
        switch self {
        case .add, .subtract, .divide, .equals:
            return Color(red: 1.0, green: 0.65, blue: 0.0)
        case .clear:
            return Color(red: 0.6, green: 0.6, blue: 0.6)
        default:
            return Color(red: 0.3, green: 0.3, blue: 0.32)
        }
    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
    }
}
