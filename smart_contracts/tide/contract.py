# pyright: reportMissingModuleSource=false
"""TIDE - a recurring micro-allowance (pull payment stream) on Algorand TestNet.

The owner funds the app, names a beneficiary once, and sets a `drip`
(uALGO matured per keeper tick). The Arcron keeper network calls `tick()`
on an hourly cadence; each tick matures one more drip. The beneficiary
*pulls* the accumulated allowance with `claim()` whenever they like -
the contract never pushes. The keeper matures the tide; the beneficiary
pulls.

Why pull, not push:

  * Inner payments are visible. `claim()` shows exactly how an ARC4 app
    pays out of its own balance with an inner transaction, and how the
    outer caller must pool the fee (flat fee >= 2000 uALGO: 1000 outer +
    1000 inner).
  * Push would couple the keeper cadence to the beneficiary's liveness
    and spend fees every tick. Pull spends one fee, when the beneficiary
    actually wants the money.

The keeper hook is fail-soft by design (see the traps list in README.md):

  * Zero-argument hook. `tick()` takes no args; Arcron supplies none.
    A keeper decides *when* tick runs, never *what* it pays.
  * Authorization is Application(keeper).address - the sender of Arcron's
    inner call. Never compare against itob(keeper_app_id); that is 8
    bytes, not an address.
  * FAIL SOFT. A hook that rejects gets backed off by keeper bots (1, 2,
    4... intervals) until the schedule quietly stops and burns escrow on
    retries. After the two authorization asserts, every no-work path here
    RETURNS 0 - beneficiary unset or pool underfunded, both return 0.
    Nothing asserts once the keeper is authenticated.
  * Zero create args. A uint64 create_arg is how a sloppy deploy script
    confuses the keeper app id with a cadence and locks an interval at
    ~68 years. There is nothing to pass at create; the keeper is named
    once via `set_keeper`, the drip by the owner via `set_drip`.

CADENCE NOTE: a tick only matures one drip, so the allowance accrues at
the keeper's cadence. With an upkeep interval of 1286 rounds (~1 h at
~2.8 s/round) the drip is effectively hourly. A missed tick is a missed
drip - the stream is best-effort, not catch-up; SKIP_AHEAD is the right
skip policy. See README.md.

TestNet only. Unaudited. Not deployed (appId = 0 until a human deploys).
"""

from typing import Final

from algopy import (
    ARC4Contract,
    Account,
    Application,
    Global,
    GlobalState,
    Txn,
    UInt64,
    gtxn,
    itxn,
)
from algopy.arc4 import abimethod

# Smallest drip `set_drip` accepts, in uALGO. The floor keeps the stream
# meaningful: below 1000 uALGO per tick a claim's pooled fee rivals the
# payout and the stream is dust in practice.
MIN_DRIP: Final = 1000

# uALGO reserved on the app address at all times. `tick` refuses to
# mature a drip the pool cannot cover on top of this floor (the account
# min-balance for the app's global schema sits inside it).
APP_MIN_BALANCE: Final = 100000


class Tide(ARC4Contract):
    """Recurring micro-allowance, matured by Arcron keepers, pulled by
    the beneficiary.

    TestNet only. Unaudited. Not a product.
    """

    def __init__(self) -> None:
        # App id of the Arcron keeper allowed to call `tick`. Zero until
        # `set_keeper`. Not an interval. Not a create arg.
        self.keeper_app = GlobalState(UInt64(0))
        # uALGO matured into the claimable pool per keeper tick. Owner-set,
        # re-settable, floored at MIN_DRIP.
        self.drip = GlobalState(UInt64(0))
        # Unclaimed drips matured by `tick` since the last `claim`.
        self.matured = GlobalState(UInt64(0))
        # Lifetime uALGO paid out to the beneficiary via `claim`.
        self.claimed_total = GlobalState(UInt64(0))
        # The account that may set the drip and the beneficiary. The
        # creator at create time; there is deliberately no transfer method.
        self.owner = GlobalState(Account())
        # The account that may `claim`. Zero address until `set_beneficiary`
        # (one-time); `tick` fail-softs while unset so no drip matures into
        # a stream with nobody to pull it.
        self.beneficiary = GlobalState(Account())

    @abimethod(create="require")
    def create(self) -> None:
        """No-op create. Zero arguments on purpose.

        The 68-year trap: never take a uint64 create arg that a deploy
        script might map to the keeper app id. Nothing to pass here.
        """
        self.keeper_app.value = UInt64(0)
        self.drip.value = UInt64(0)
        self.matured.value = UInt64(0)
        self.claimed_total.value = UInt64(0)
        self.owner.value = Txn.sender
        self.beneficiary.value = Account()

    @abimethod()
    def set_keeper(self, keeper: Application) -> None:
        """Name the Arcron keeper whose app account may call `tick`.

        Creator-only, one-time. Pass the keeper *application*, not a raw
        uint64. `tick` authorizes Application(keeper).address - the
        inner-call sender when Arcron `execute()` inner-calls this app -
        never itob(keeper.id). Puya lowers the Application param to uint64
        in the ABI signature; the compiled selector is set_keeper(uint64)void.
        """
        assert Txn.sender == Global.creator_address, "Only the creator can set the keeper"
        assert self.keeper_app.value == 0, "Keeper already set"
        assert keeper.id != 0, "Keeper app required"
        self.keeper_app.value = keeper.id

    @abimethod()
    def set_beneficiary(self, beneficiary: Account) -> None:
        """Name the account allowed to pull the allowance via `claim`.

        Owner-only, one-time. There is deliberately no re-point method:
        the stream pays whoever the owner named at the start, and a
        one-time name keeps that promise simple. Puya lowers the Account
        param to address in the ABI signature; the compiled selector is
        set_beneficiary(address)void.
        """
        assert Txn.sender == self.owner.value, "Only the owner can set the beneficiary"
        assert self.beneficiary.value == Global.zero_address, "Beneficiary already set"
        self.beneficiary.value = beneficiary

    @abimethod()
    def set_drip(self, drip: UInt64) -> None:
        """Set (or re-set) the uALGO matured per keeper tick.

        Owner-only, re-settable: the owner may raise or lower the stream
        at will, floored at MIN_DRIP (1000 uALGO) so a claim's pooled fee
        never rivals the payout. Applies from the next `tick`; drips
        already matured keep paying out at whatever rate they matured.
        """
        assert Txn.sender == self.owner.value, "Only the owner can set the drip"
        assert drip >= MIN_DRIP, "Drip below floor"
        self.drip.value = drip

    @abimethod()
    def fund(self, payment: gtxn.PaymentTransaction) -> None:
        """Top up the pool. Anyone may fund; the money only ever flows
        out to the named beneficiary.

        The payment must land on the app address and carry more than
        zero uALGO. Matured drips are only matured while the pool can
        cover them (see `tick`), so funding after the fact does not
        back-pay missed ticks - it just lets future ticks mature.
        """
        assert payment.receiver == Global.current_application_address, "Payment must go to the app"
        assert payment.amount > 0, "Payment must be nonzero"

    @abimethod()
    def tick(self) -> UInt64:
        """Arcron hook. Zero arguments; the selector is the only app arg.

        Returns the new `matured` count when a drip matures, 0 on every
        no-work path. FAIL SOFT: after the two authorization asserts
        nothing here may reject - a failing hook gets exponentially
        backed off by keeper bots and burns upkeep escrow on retries.

        No-work paths, all returning 0:
          * beneficiary unset - a stream with nobody to pull it does not
            mature;
          * pool underfunded - the app balance minus the APP_MIN_BALANCE
            floor (100000 uALGO) cannot cover drip * (matured + 1), so
            one more drip would promise money the pool cannot pay.

        Best-effort, not catch-up: a missed or skipped tick is a missed
        drip. Register with SKIP_AHEAD; see README.md.
        """
        keeper = self.keeper_app.value
        assert keeper != 0, "Keeper not set"
        # Inner-call sender is the keeper *app account*, not itob(keeper.id).
        assert (
            Txn.sender == Application(keeper).address
        ), "Only the keeper app may tick"

        # Nobody to pull the stream yet. Return, do not assert.
        if self.beneficiary.value == Global.zero_address:
            return UInt64(0)

        # Underfunded pool: never mature a drip the app cannot pay.
        # (The app exists, so its balance is always >= APP_MIN_BALANCE.)
        drip = self.drip.value
        owed = drip * (self.matured.value + 1)
        if Global.current_application_address.balance - APP_MIN_BALANCE < owed:
            return UInt64(0)

        # Mature one drip.
        self.matured.value += 1
        return self.matured.value

    @abimethod()
    def claim(self) -> None:
        """Pull the matured allowance. Beneficiary-only.

        Pays matured * drip uALGO out of the app balance with an inner
        payment, adds it to `claimed_total`, and zeroes `matured`. If
        nothing has matured, returns quietly (fail-soft; a no-op claim
        is harmless).

        FEE POOLING: the inner payment carries a 1000 uALGO fee, so the
        outer call must set a flat fee >= 2000 uALGO (1000 outer + 1000
        inner). A claim sent with the default 1000 uALGO fee fails on
        fee underspend. This is the pull-payment trade the README calls
        out: one pooled fee, paid by the beneficiary, only when they
        actually pull.
        """
        assert Txn.sender == self.beneficiary.value, "Only the beneficiary can claim"
        matured = self.matured.value
        if matured == 0:
            return
        amount = matured * self.drip.value
        itxn.Payment(
            receiver=self.beneficiary.value,
            amount=amount,
            fee=1000,
        ).submit()
        self.claimed_total.value += amount
        self.matured.value = UInt64(0)

    @abimethod(readonly=True)
    def claimable(self) -> UInt64:
        """uALGO the beneficiary could pull right now: matured * drip."""
        return self.matured.value * self.drip.value
