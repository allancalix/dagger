# This file generated by `mix dagger.gen`. Please DO NOT EDIT.
defmodule Dagger.FunctionCallArgValue do
  @moduledoc "FunctionCallArgValue"
  @type t() :: %__MODULE__{name: Dagger.String.t(), value: Dagger.JSON.t()}
  @derive Nestru.Decoder
  defstruct [:name, :value]
end
